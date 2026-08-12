"""Session-Momentum Expansion (SME) v1 -- three stacked layers, all must agree.

Context (Layer A, once at 10:00 ET): the opening half-hour must have moved
meaningfully relative to recent opens (|OR_ret| >= K_bias * sigma_or) to
establish a one-directional bias for the day; an overnight session that
already travelled most of a normal day's range stands the day down.

Trigger (Layer B, 10:00-15:30 ET): stop-entry beyond the opening range with a
volatility-scaled buffer (B_mult * ATR5). Initial stop at OR mid, capped at
S_mult * ATR5 from entry, whichever is closer. No fixed profit target -- the
edge is the tail -- exits are break-even at +1R, then a 5m-bar trail, then a
hard 15:55 flatten.

Gate (Layer C, at trigger touch): order-flow imbalance must confirm the
breakout direction, thresholded at a percentile of its own trailing
distribution (calibrated weekly from the trailing 60 sessions, zero
lookahead).

Deviations from the spec (v1, in this engine) -- each is a deliberate
adaptation to this codebase's seams, listed so the backtest is read honestly:

- ENTRY signals require a target, so "no target" is a far target
  (`far_target_points`, default 1000 NQ points) the session cannot reach.
- The engine has no stop-modify: the initial stop is real (server-side
  bracket, enforced by PositionTracker); break-even and the 5m trail are a
  VIRTUAL stop inside the strategy that emits FLATTEN when a bar violates
  it, filling at that bar's close rather than at the trail price. The $10/RT
  cost model plus the +1-tick adverse entry fill keep the model conservative
  overall.
- Bars carry no aggressor side, so OFI_MODE "full" is unavailable; "proxy"
  is the close-position-weighted volume delta on 1m bars per spec 5.2, and
  "off" runs A+B alone for the ablation. "full" becomes possible when the
  data layer carries per-bar buy/sell volume.
- Sizing is a constant `quantity` (default 1): the drawdown-buffer formula
  needs account state a pure strategy cannot see, and the risk layer already
  owns the money limits. Backtest EV is per-contract either way.
- The exhaustion filter needs overnight data. Sessions are ET calendar
  dates in this engine, so at most the 00:00-09:30 portion of the overnight
  session is visible; the filter uses what it sees and is inert on RTH-only
  fixtures (understating ON_range weakens the filter, never the entries).
- Entry timing is bar-close granularity: the trigger-touch and gate read
  happen on the 1m bar that touched, not at a 5-second cadence.
- The economic calendar arrives as constructor arguments (`no_trade_dates`,
  `fomc_dates`, `blackout_times`); defaults are empty, so calendar
  enforcement is only as good as the dates supplied.
- No partial exits: the tracker closes all-or-nothing, and the spec ships
  v1 with partials OFF anyway.

All rolling statistics are trailing and exclude the current day at the moment
they are consulted. Until `stat_window` completed sessions exist, the context
layer refuses to trade (warmup); the OFI gate likewise fails closed until it
has `ofi_min_sessions` of samples to calibrate on. Both come alive together
around day `stat_window` + 1 of a run, so the A+B and A+B+C ablations start
trading on comparable dates.
"""

from __future__ import annotations

import statistics
from datetime import date, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.strategy.base import Strategy

ET = ZoneInfo("America/New_York")

# Day states. Strings, not an Enum, because they round-trip through get_state.
IDLE = "idle"  # before the 10:00 bias evaluation
ARMED = "armed"  # bias set, trigger live, waiting for touch + gate
PENDING = "pending"  # entry emitted, waiting one bar to learn whether it filled
IN_TRADE = "in_trade"  # position on, managing the virtual stop
WAIT_REARM = "wait_rearm"  # stopped/disarmed; price must re-enter the OR first
DONE = "done"  # no further entries today


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _percentile(samples: list[float], pct: int) -> float:
    """Nearest-rank percentile. Deterministic and interpolation-free."""
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil without floats
    return ordered[rank - 1]


class SessionMomentumExpansion(Strategy):
    name = "sme"
    required_timeframes = ["1m", "5m"]

    def __init__(
        self,
        *,
        k_bias: Decimal = Decimal("0.35"),
        k_exh: Decimal = Decimal("0.85"),
        b_mult: Decimal = Decimal("0.10"),
        s_mult: Decimal = Decimal("1.25"),
        r_max_points: Decimal = Decimal("60"),
        n_ofi: int = 5,
        q_ofi_percentile: int = 70,
        ofi_mode: str = "proxy",
        layer_a: bool = True,
        quantity: int = 1,
        compression_mult: Decimal = Decimal("0.65"),
        chase_mult: Decimal = Decimal("0.5"),
        rearm_depth: Decimal = Decimal("0.25"),
        far_target_points: Decimal = Decimal("1000"),
        stat_window: int = 20,
        ofi_window_sessions: int = 60,
        ofi_min_sessions: int = 20,
        max_entries_per_day: int = 2,
        tick_size: Decimal = Decimal("0.25"),
        no_trade_dates: set[date] | None = None,
        fomc_dates: set[date] | None = None,
        blackout_times: dict[date, list[time]] | None = None,
    ) -> None:
        if ofi_mode not in ("proxy", "off"):
            # "full" is a spec mode this data layer cannot honour yet; failing
            # loudly beats silently running proxy under a "full" label.
            raise ValueError(f"ofi_mode must be 'proxy' or 'off' in v1, got {ofi_mode!r}")
        if quantity < 1:
            raise ValueError(f"quantity must be at least 1, got {quantity}")
        if stat_window < 2:
            raise ValueError(f"stat_window must be at least 2, got {stat_window}")
        self._k_bias = k_bias
        self._k_exh = k_exh
        self._b_mult = b_mult
        self._s_mult = s_mult
        self._r_max = r_max_points
        self._n_ofi = n_ofi
        self._q_pct = q_ofi_percentile
        self._ofi_mode = ofi_mode
        self._layer_a = layer_a
        self._quantity = quantity
        self._compression_mult = compression_mult
        self._chase_mult = chase_mult
        self._rearm_depth = rearm_depth
        self._far_target = far_target_points
        self._stat_window = stat_window
        self._ofi_window_sessions = ofi_window_sessions
        self._ofi_min_sessions = ofi_min_sessions
        self._max_entries = max_entries_per_day
        self._tick = tick_size
        self._no_trade_dates = no_trade_dates or set()
        self._fomc_dates = fomc_dates or set()
        self._blackout_times = blackout_times or {}

        # --- rolling, cross-session state (the strategy's long memory) -----
        self._or_rets: list[float] = []  # trailing OR_ret, newest last
        self._or_ranges: list[float] = []  # trailing OR_range in points
        self._daily_ranges: list[float] = []  # trailing RTH high-low in points
        self._atr: Decimal | None = None  # Wilder ATR(14) on RTH 5m bars
        self._prev_5m_close: Decimal | None = None
        # Per-session |OFI| samples for gate calibration: [iso_date, [floats]]
        self._ofi_sessions: list[list[Any]] = []
        self._q_ofi: float | None = None
        self._q_week: str | None = None  # ISO year-week of the last calibration

        self._reset_day()

    # ------------------------------------------------------------------ day

    def _reset_day(self) -> None:
        self._state = IDLE
        self._bias: str | None = None  # "LONG" | "SHORT" | None (layer A off)
        self._or_high: Decimal | None = None
        self._or_low: Decimal | None = None
        self._or_open: Decimal | None = None
        self._or_close: Decimal | None = None
        self._or_recorded = False
        self._atr_at_bias: Decimal | None = None
        self._entries_today = 0
        self._rearmed = False  # the one post-stop re-arm has been used
        self._compressed = False
        self._vstop: Decimal | None = None
        self._be_done = False
        self._entry_price: Decimal | None = None
        self._risk: Decimal | None = None
        self._trades_at_entry: int | None = None
        self._live_direction: Direction | None = None
        self._day_high: Decimal | None = None
        self._day_low: Decimal | None = None
        self._on_high: Decimal | None = None
        self._on_low: Decimal | None = None
        self._today_ofi_samples: list[float] = []

    def on_session_start(self, session_date: date) -> None:
        # Day-scoped state only. The rolling statistics are the strategy's
        # memory across sessions; clearing them here would wipe 20 days of
        # calibration at every rollover of a multi-day run.
        self._reset_day()

    def on_session_end(self, session_date: date) -> None:
        if self._day_high is not None and self._day_low is not None:
            self._push(self._daily_ranges, float(self._day_high - self._day_low))
        if self._today_ofi_samples:
            self._ofi_sessions.append([session_date.isoformat(), self._today_ofi_samples])
            del self._ofi_sessions[: -self._ofi_window_sessions]

    def _push(self, series: list[float], value: float) -> None:
        series.append(value)
        del series[: -self._stat_window]

    # ------------------------------------------------------------ indicators

    def _update_atr(self, bar: Bar) -> None:
        """Wilder ATR(14) on RTH 5m bars, carried across sessions."""
        if self._prev_5m_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_5m_close),
                abs(bar.low - self._prev_5m_close),
            )
        self._prev_5m_close = bar.close
        self._atr = tr if self._atr is None else (self._atr * 13 + tr) / 14

    def _proxy_delta(self, bar: Bar) -> Decimal:
        """Close-position-weighted volume delta: where in its range a bar
        closed decides how much of its volume reads as buying vs selling."""
        span = bar.high - bar.low
        if span == 0:
            return Decimal("0")
        weight = (bar.close - bar.low) * 2 / span - 1
        return weight * bar.volume

    def _ofi(self, context: Context) -> Decimal | None:
        bars = list(context.bars("1m", self._n_ofi))
        if len(bars) < self._n_ofi:
            return None
        total = sum(b.volume for b in bars)
        if total == 0:
            return None
        delta = sum((self._proxy_delta(b) for b in bars), Decimal("0"))
        return delta / total

    def _calibrate_gate(self, session_date: date) -> None:
        """Weekly Q_ofi recalibration off the trailing sessions' samples.

        Trailing only: today's samples are appended at session end, so the
        threshold consulted at any moment was computed entirely from
        completed sessions.
        """
        iso = session_date.isocalendar()
        week = f"{iso.year}-{iso.week}"
        if self._q_week == week and self._q_ofi is not None:
            return
        if len(self._ofi_sessions) < self._ofi_min_sessions:
            return  # stays uncalibrated; the gate fails closed
        recent = self._ofi_sessions[-self._ofi_window_sessions :]
        samples = [value for _, day_samples in recent for value in day_samples]
        if not samples:
            return
        self._q_ofi = _percentile(samples, self._q_pct)
        self._q_week = week

    def _gate_passes(self, context: Context, direction: str) -> bool:
        if self._ofi_mode == "off":
            return True
        if self._q_ofi is None:
            return False  # an uncalibrated gate does not wave trades through
        reading = self._ofi(context)
        if reading is None:
            return False
        threshold = Decimal(str(self._q_ofi))
        if direction == "LONG":
            return reading >= threshold
        return reading <= -threshold

    # --------------------------------------------------------------- layer A

    def _evaluate_bias(self, session_date: date) -> None:
        """Once, on the first 1m bar closing at/after 10:00 ET."""
        self._or_recorded = True
        or_ret: float | None = None
        if (
            self._or_open is not None
            and self._or_close is not None
            and self._or_open != 0
            and self._or_high is not None
            and self._or_low is not None
        ):
            or_ret = float((self._or_close - self._or_open) / self._or_open)

        armed = self._decide(session_date, or_ret)

        # Append today's OR stats AFTER deciding: the decision must only see
        # completed days, and tomorrow's decision must see today.
        if or_ret is not None:
            assert self._or_high is not None and self._or_low is not None
            self._push(self._or_rets, or_ret)
            self._push(self._or_ranges, float(self._or_high - self._or_low))

        self._state = ARMED if armed else DONE

    def _decide(self, session_date: date, or_ret: float | None) -> bool:
        if session_date in self._no_trade_dates:
            return False
        if or_ret is None or self._or_high is None or self._or_low is None:
            return False  # no opening range means nothing to break out of
        if self._atr is None or self._atr <= 0:
            return False

        if not self._layer_a:
            # Ablation "B alone": no directional bias, both triggers live.
            self._bias = None
            self._atr_at_bias = self._atr
            return True

        if len(self._or_rets) < self._stat_window or len(self._daily_ranges) < self._stat_window:
            return False  # warmup: not enough completed sessions to compare against

        sigma = statistics.stdev(self._or_rets)
        if sigma <= 0:
            return False
        threshold = float(self._k_bias) * sigma
        if or_ret >= threshold:
            self._bias = "LONG"
        elif or_ret <= -threshold:
            self._bias = "SHORT"
        else:
            return False  # neutral open: stand down (v1, strictest)

        # Exhaustion: an overnight that already spent the day's range. Only
        # evaluable when overnight bars were in the data -- see module note.
        avg_range = statistics.fmean(self._daily_ranges)
        if self._on_high is not None and self._on_low is not None and avg_range > 0:
            on_range = float(self._on_high - self._on_low)
            if on_range >= float(self._k_exh) * avg_range:
                return False

        # Compression tailwind: informational in v1, logged via metadata.
        if len(self._or_ranges) >= self._stat_window:
            med = statistics.median(self._or_ranges)
            or_range = float(self._or_high - self._or_low)
            self._compressed = or_range < float(self._compression_mult) * med

        # ATR frozen at bias time: the levels a day trades are decided once,
        # not levels that drift with every subsequent 5m bar.
        self._atr_at_bias = self._atr
        return True

    def _triggers(self) -> list[tuple[str, Decimal]]:
        assert self._or_high is not None and self._or_low is not None
        assert self._atr_at_bias is not None
        buffer = self._b_mult * self._atr_at_bias
        if self._bias == "LONG":
            return [("LONG", self._or_high + buffer)]
        if self._bias == "SHORT":
            return [("SHORT", self._or_low - buffer)]
        return [("LONG", self._or_high + buffer), ("SHORT", self._or_low - buffer)]

    # --------------------------------------------------------------- layer B

    def _try_enter(self, bar: Bar, context: Context) -> Signal | None:
        for direction, trigger in self._triggers():
            touched = bar.high >= trigger if direction == "LONG" else bar.low <= trigger
            if not touched:
                continue
            if not self._gate_passes(context, direction):
                # Gate failed at the touch. Stay armed unless price has run
                # too far beyond the trigger to enter without chasing.
                assert self._atr_at_bias is not None
                chase = self._chase_mult * self._atr_at_bias
                ran_away = (
                    bar.close > trigger + chase
                    if direction == "LONG"
                    else bar.close < trigger - chase
                )
                if ran_away:
                    self._to_wait_rearm()
                return None
            return self._enter(bar, direction, trigger)
        return None

    def _enter(self, bar: Bar, direction: str, trigger: Decimal) -> Signal | None:
        assert self._or_high is not None and self._or_low is not None
        assert self._atr_at_bias is not None
        or_mid = (self._or_high + self._or_low) / 2
        if direction == "LONG":
            # A bar opening beyond the trigger is a gap: the stop-market
            # fills at the first traded price, not the trigger. One tick of
            # adverse slippage on top, per the cost model.
            fill = max(bar.open, trigger) + self._tick
            stop = max(or_mid, fill - self._s_mult * self._atr_at_bias)
            risk = fill - stop
            target = fill + self._far_target
            side = Direction.LONG
        else:
            fill = min(bar.open, trigger) - self._tick
            stop = min(or_mid, fill + self._s_mult * self._atr_at_bias)
            risk = stop - fill
            target = fill - self._far_target
            side = Direction.SHORT

        if risk <= 0 or risk > self._r_max:
            # Too wide to risk-define (or degenerate). The OR geometry will
            # not change intraday, so the day is over, not just the attempt.
            self._state = DONE
            return None

        self._state = PENDING
        self._entry_price = fill
        self._vstop = stop
        self._risk = risk
        self._be_done = False
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            direction=side,
            entry_price=fill,
            stop_price=stop,
            target_price=target,
            quantity=self._quantity,
            reason=(
                f"SME expansion {direction.lower()}: trigger {trigger} touched, "
                f"R={risk} points"
            ),
            metadata={
                "strategy": self.name,
                "trigger": str(trigger),
                "or_high": str(self._or_high),
                "or_low": str(self._or_low),
                "atr5": str(self._atr_at_bias),
                "risk_points": str(risk),
                "compressed": self._compressed,
                "entry_number": self._entries_today + 1,
            },
        )

    def _to_wait_rearm(self) -> None:
        """A consumed attempt. One re-entry cycle is allowed per day, and only
        after price trades back INSIDE the opening range by `rearm_depth`."""
        if self._rearmed or self._entries_today >= self._max_entries:
            self._state = DONE
            return
        self._rearmed = True
        self._state = WAIT_REARM

    def _check_rearm(self, bar: Bar) -> None:
        assert self._or_high is not None and self._or_low is not None
        depth = self._rearm_depth * (self._or_high - self._or_low)
        if self._bias in (None, "LONG") and bar.low <= self._or_high - depth:
            self._state = ARMED
            return
        if self._bias in (None, "SHORT") and bar.high >= self._or_low + depth:
            self._state = ARMED

    # ------------------------------------------------------------ management

    def _exit_signal(self, bar: Bar, direction: Direction, why: str) -> Signal:
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            intent=SignalIntent.FLATTEN,
            direction=direction,
            quantity=self._quantity,
            reason=f"SME exit: {why}",
        )

    def _clear_trade(self) -> None:
        self._entry_price = None
        self._risk = None
        self._vstop = None
        self._be_done = False
        self._live_direction = None

    def _manage(self, bar: Bar, context: Context, et_time: time) -> Signal | None:
        position = context.position
        if position is None:
            # The engine closed it before this bar reached us -- with a far
            # target, that is the hard stop. Re-arm rules apply.
            self._clear_trade()
            self._to_wait_rearm()
            if self._state == WAIT_REARM:
                self._check_rearm(bar)
            return None

        assert self._entry_price is not None and self._risk is not None
        assert self._vstop is not None
        long = position.direction is Direction.LONG
        session_date = bar.close_time.astimezone(ET).date()

        # Hard time exits before anything else. 15:55 always; 13:45 on FOMC
        # days -- both are the spec's own clock, well inside the engine's
        # session-cutoff flatten, which stays as the backstop.
        if et_time >= time(15, 55) or (
            session_date in self._fomc_dates and et_time >= time(13, 45)
        ):
            direction = position.direction
            self._clear_trade()
            self._state = DONE
            return self._exit_signal(bar, direction, "time")

        # Break-even: one full R of favourable excursion moves the stop to
        # entry +/- 2 ticks. Measured on the bar's extreme, the way a live
        # bot watching the tape would see it.
        if not self._be_done:
            excursion = (bar.high - self._entry_price) if long else (self._entry_price - bar.low)
            if excursion >= self._risk:
                be = (
                    self._entry_price + 2 * self._tick
                    if long
                    else self._entry_price - 2 * self._tick
                )
                self._vstop = max(self._vstop, be) if long else min(self._vstop, be)
                self._be_done = True

        # Virtual stop violated? The real (server-side) stop is the initial
        # one; anything tighter is ours to enforce, and only when it is
        # actually tighter -- the tracker already owns the initial stop.
        hard_stop = position.stop_price
        tighter = (
            hard_stop is None or (self._vstop > hard_stop if long else self._vstop < hard_stop)
        )
        violated = bar.low <= self._vstop if long else bar.high >= self._vstop
        if violated and tighter:
            direction = position.direction
            banked = self._be_done
            self._clear_trade()
            if banked:
                # A trailed exit means the expansion ran and gave back; that
                # move is spent. No re-entry chasing the same leg.
                self._state = DONE
            else:
                self._to_wait_rearm()
            return self._exit_signal(bar, direction, "virtual stop")

        return None

    def _trail(self, five_min_bar: Bar) -> None:
        """After break-even, ratchet the virtual stop behind each completed
        5m bar. Monotonic: it tightens or holds, never loosens."""
        if self._state != IN_TRADE or not self._be_done or self._vstop is None:
            return
        if self._live_direction is Direction.LONG:
            self._vstop = max(self._vstop, five_min_bar.low - self._tick)
        elif self._live_direction is Direction.SHORT:
            self._vstop = min(self._vstop, five_min_bar.high + self._tick)

    # ------------------------------------------------------------------ main

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        et = bar.close_time.astimezone(ET)
        et_time = et.time()
        session_date = et.date()

        if bar.timeframe == "5m":
            if time(9, 30) < et_time <= time(16, 30):
                self._update_atr(bar)
                # Gate calibration samples: |OFI| at 5m closes inside the
                # entry window, banked for future sessions' thresholds.
                if self._ofi_mode != "off" and time(10, 0) <= et_time <= time(15, 30):
                    reading = self._ofi(context)
                    if reading is not None:
                        self._today_ofi_samples.append(abs(float(reading)))
                self._trail(bar)
            return None

        if bar.timeframe != "1m":
            return None

        # Overnight accumulation (whatever portion the data carries).
        if et_time <= time(9, 30):
            self._on_high = bar.high if self._on_high is None else max(self._on_high, bar.high)
            self._on_low = bar.low if self._on_low is None else min(self._on_low, bar.low)
            return None

        # Day range, for the rolling daily-range statistic.
        self._day_high = bar.high if self._day_high is None else max(self._day_high, bar.high)
        self._day_low = bar.low if self._day_low is None else min(self._day_low, bar.low)

        # Opening range: 1m bars closing in (09:30, 10:00].
        if et_time <= time(10, 0):
            if self._or_open is None:
                self._or_open = bar.open
            self._or_close = bar.close
            self._or_high = bar.high if self._or_high is None else max(self._or_high, bar.high)
            self._or_low = bar.low if self._or_low is None else min(self._or_low, bar.low)
            if et_time == time(10, 0) and not self._or_recorded:
                self._calibrate_gate(session_date)
                self._evaluate_bias(session_date)
            return None

        if not self._or_recorded:
            # The 10:00 bar was missing from the data; evaluate on the first
            # bar past it with whatever range accumulated.
            self._calibrate_gate(session_date)
            self._evaluate_bias(session_date)

        # Resolve a pending entry before anything else: did it actually fill?
        if self._state == PENDING:
            assert self._trades_at_entry is not None
            if context.trades_taken > self._trades_at_entry:
                self._entries_today += 1
                if context.position is not None:
                    self._state = IN_TRADE
                    self._live_direction = context.position.direction
                else:
                    # Filled and closed within a single bar: a same-bar stop.
                    self._clear_trade()
                    self._to_wait_rearm()
            else:
                # The risk layer refused it. It said no for a reason; the
                # strategy does not argue with the governor.
                self._clear_trade()
                self._state = DONE
            self._trades_at_entry = None

        if self._state == IN_TRADE:
            return self._manage(bar, context, et_time)

        if self._state == WAIT_REARM:
            self._check_rearm(bar)

        if self._state != ARMED:
            return None

        # Entry window and calendar blackouts.
        if et_time > time(15, 30):
            self._state = DONE
            return None
        if session_date in self._fomc_dates and et_time >= time(13, 45):
            self._state = DONE
            return None
        for release in self._blackout_times.get(session_date, []):
            release_minutes = release.hour * 60 + release.minute
            bar_minutes = et_time.hour * 60 + et_time.minute
            if 0 <= release_minutes - bar_minutes <= 3:
                return None
        if context.is_warmup:
            # Backfill replay: the engine would suppress the signal anyway;
            # not emitting keeps this state machine agreeing with the
            # tracker about whether a position exists.
            return None
        if context.position is not None:
            return None  # the engine holds something this strategy did not open

        signal = self._try_enter(bar, context)
        if signal is not None:
            self._trades_at_entry = context.trades_taken
        return signal

    # ----------------------------------------------------------------- state

    def get_state(self) -> dict[str, Any]:
        return {
            "or_rets": self._or_rets,
            "or_ranges": self._or_ranges,
            "daily_ranges": self._daily_ranges,
            "atr": None if self._atr is None else str(self._atr),
            "prev_5m_close": None if self._prev_5m_close is None else str(self._prev_5m_close),
            "ofi_sessions": self._ofi_sessions,
            "q_ofi": self._q_ofi,
            "q_week": self._q_week,
            "state": self._state,
            "bias": self._bias,
            "or_high": None if self._or_high is None else str(self._or_high),
            "or_low": None if self._or_low is None else str(self._or_low),
            "or_open": None if self._or_open is None else str(self._or_open),
            "or_close": None if self._or_close is None else str(self._or_close),
            "or_recorded": self._or_recorded,
            "atr_at_bias": None if self._atr_at_bias is None else str(self._atr_at_bias),
            "entries_today": self._entries_today,
            "rearmed": self._rearmed,
            "compressed": self._compressed,
            "vstop": None if self._vstop is None else str(self._vstop),
            "be_done": self._be_done,
            "entry_price": None if self._entry_price is None else str(self._entry_price),
            "risk": None if self._risk is None else str(self._risk),
            "trades_at_entry": self._trades_at_entry,
            "live_direction": (
                None if self._live_direction is None else self._live_direction.value
            ),
            "day_high": None if self._day_high is None else str(self._day_high),
            "day_low": None if self._day_low is None else str(self._day_low),
            "on_high": None if self._on_high is None else str(self._on_high),
            "on_low": None if self._on_low is None else str(self._on_low),
            "today_ofi_samples": self._today_ofi_samples,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        # Coerced, not assigned: everything here came back through JSON.
        self._or_rets = [float(v) for v in state.get("or_rets", [])]
        self._or_ranges = [float(v) for v in state.get("or_ranges", [])]
        self._daily_ranges = [float(v) for v in state.get("daily_ranges", [])]
        self._atr = _dec(state.get("atr"))
        self._prev_5m_close = _dec(state.get("prev_5m_close"))
        self._ofi_sessions = [
            [str(day), [float(s) for s in samples]]
            for day, samples in state.get("ofi_sessions", [])
        ]
        q = state.get("q_ofi")
        self._q_ofi = None if q is None else float(q)
        week = state.get("q_week")
        self._q_week = None if week is None else str(week)
        self._state = str(state.get("state", IDLE))
        bias = state.get("bias")
        self._bias = None if bias is None else str(bias)
        self._or_high = _dec(state.get("or_high"))
        self._or_low = _dec(state.get("or_low"))
        self._or_open = _dec(state.get("or_open"))
        self._or_close = _dec(state.get("or_close"))
        self._or_recorded = bool(state.get("or_recorded", False))
        self._atr_at_bias = _dec(state.get("atr_at_bias"))
        self._entries_today = int(state.get("entries_today", 0))
        self._rearmed = bool(state.get("rearmed", False))
        self._compressed = bool(state.get("compressed", False))
        self._vstop = _dec(state.get("vstop"))
        self._be_done = bool(state.get("be_done", False))
        self._entry_price = _dec(state.get("entry_price"))
        self._risk = _dec(state.get("risk"))
        raw_trades = state.get("trades_at_entry")
        self._trades_at_entry = None if raw_trades is None else int(raw_trades)
        raw_direction = state.get("live_direction")
        self._live_direction = None if raw_direction is None else Direction(str(raw_direction))
        self._day_high = _dec(state.get("day_high"))
        self._day_low = _dec(state.get("day_low"))
        self._on_high = _dec(state.get("on_high"))
        self._on_low = _dec(state.get("on_low"))
        self._today_ofi_samples = [float(s) for s in state.get("today_ofi_samples", [])]
