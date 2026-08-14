"""Noise-Area Intraday Momentum (NAIM) v1 -- spec v1.0.

The boundary is statistical, not a chart level: at each minute t the day's
"normal noise" is sigma(t), the trailing-L-session mean of the absolute
fractional move from the open at that minute. Price closing beyond the
gap-adjusted noise band is the abnormal move the strategy rides; the exit
posture is hold-to-close (the SME lesson, and Gao et al.'s late-day
persistence), invalidated only by price closing back inside the noise
regime (band/VWAP structural stop) or by the catastrophic hard stop.

Layers: noise-band trigger (core, external evidence) -> OFI gate (in-house
component, must re-earn its value in this strategy's ablation) -> exits.

Deviations from spec v1.0 in this engine, each deliberate:

- Entry fills are modeled at trigger-bar close + 1 tick adverse. The spec
  says next-bar open + 1 tick; on 1m bars those differ by at most the
  close-to-open drift of one minute, and the engine fills at signal time.
- The structural stop is strategy-emitted FLATTEN, filled at the violating
  bar's close -- which is exactly the spec's close-based exit semantics.
  Touch-based stop mode exits on the bar whose extreme touched the level,
  still filled at that bar's close (conservative for real touches).
- Stop-and-reverse is two steps: the opposite trigger flattens on its bar;
  the entry happens on a later bar if the band is still exceeded and the
  gate passes. One signal per bar is an engine invariant.
- The catastrophic stop needs a target for the Signal contract, so a far
  target (1000 points) stands in for "none", as in SME.
- Sizing is a constant `quantity` (default 1); per-contract economics, as
  reported for SME. The spec's buffer-based formula is live wiring.
- sigma(t) curves arrive precomputed (scripts/precompute_noise.py) via the
  constructor, keyed by session date. A session with no curve is a no-trade
  day (warmup). The strategy uses its own observed first-bar open for the
  anchors and banks the prior session's close itself; the curve file's
  prev_close is the cold-start fallback.

OFI machinery (proxy delta, weekly percentile calibration, fail-closed
uncalibrated gate) is copied from sme.py rather than shared, so neither
strategy's tuning can silently change the other.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.strategy.base import Strategy

ET = ZoneInfo("America/New_York")

IDLE = "idle"  # no curve or before first RTH bar
ACTIVE = "active"  # flat, triggers armed
PENDING = "pending"  # entry emitted, awaiting fill confirmation
IN_TRADE = "in_trade"
DONE = "done"  # no further entries today


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _percentile(samples: list[float], pct: int) -> float:
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * pct // 100))
    return ordered[rank - 1]


class NoiseAreaIntradayMomentum(Strategy):
    name = "naim"
    required_timeframes = ["1m", "5m"]

    def __init__(
        self,
        *,
        noise_curves: dict[str, dict[str, Any]] | None = None,
        trigger_mode: str = "1m",  # "1m" | "30m" (evaluate only at HH:00/HH:30)
        ofi_mode: str = "off",  # "off" | "proxy"
        n_ofi: int = 5,
        q_ofi_percentile: int = 70,
        ofi_window_sessions: int = 60,
        ofi_min_sessions: int = 20,
        stop_mode: str = "close",  # "close" | "touch"
        vwap_stop: bool = True,
        entry_end: time = time(15, 0),
        max_entries_per_day: int = 4,
        r_max_points: Decimal = Decimal("80"),
        far_target_points: Decimal = Decimal("1000"),
        quantity: int = 1,
        tick_size: Decimal = Decimal("0.25"),
        fomc_dates: set[date] | None = None,
        no_trade_dates: set[date] | None = None,
    ) -> None:
        if trigger_mode not in ("1m", "30m"):
            raise ValueError(f"trigger_mode must be '1m' or '30m', got {trigger_mode!r}")
        if ofi_mode not in ("proxy", "off"):
            raise ValueError(f"ofi_mode must be 'proxy' or 'off' in v1, got {ofi_mode!r}")
        if stop_mode not in ("close", "touch"):
            raise ValueError(f"stop_mode must be 'close' or 'touch', got {stop_mode!r}")
        # Same shared-reference rule as TFR's decisions book (see tfr.py).
        self._curves = noise_curves if noise_curves is not None else {}
        self._trigger_mode = trigger_mode
        self._ofi_mode = ofi_mode
        self._n_ofi = n_ofi
        self._q_pct = q_ofi_percentile
        self._ofi_window_sessions = ofi_window_sessions
        self._ofi_min_sessions = ofi_min_sessions
        self._stop_mode = stop_mode
        self._vwap_stop = vwap_stop
        self._entry_end = entry_end
        self._max_entries = max_entries_per_day
        self._r_max = r_max_points
        self._far_target = far_target_points
        self._quantity = quantity
        self._tick = tick_size
        self._fomc_dates = fomc_dates or set()
        self._no_trade_dates = no_trade_dates or set()

        # Cross-session memory.
        self._prev_close: Decimal | None = None
        self._ofi_sessions: list[list[Any]] = []
        self._q_ofi: float | None = None
        self._q_week: str | None = None

        self._reset_day()

    # ------------------------------------------------------------------ day

    def _reset_day(self) -> None:
        self._state = IDLE
        self._open: Decimal | None = None
        self._sigma: dict[int, float] = {}
        self._anchor_up: Decimal | None = None
        self._anchor_dn: Decimal | None = None
        self._vwap_pv = Decimal("0")
        self._vwap_v = 0
        self._entries_today = 0
        self._trades_at_entry: int | None = None
        self._live_direction: Direction | None = None
        self._today_close: Decimal | None = None
        self._today_ofi_samples: list[float] = []

    def on_session_start(self, session_date: date) -> None:
        self._reset_day()

    def on_session_end(self, session_date: date) -> None:
        if self._today_close is not None:
            self._prev_close = self._today_close
        if self._today_ofi_samples:
            self._ofi_sessions.append([session_date.isoformat(), self._today_ofi_samples])
            del self._ofi_sessions[: -self._ofi_window_sessions]

    # ----------------------------------------------------------------- bands

    def _minute_index(self, et_hour: int, et_minute: int) -> int:
        return (et_hour - 9) * 60 + et_minute - 30

    def _bounds(self, index: int) -> tuple[Decimal, Decimal] | None:
        """(UB_t, LB_t) at minute index, or None when no sigma is known."""
        sigma = self._sigma.get(index)
        if sigma is None or self._anchor_up is None or self._anchor_dn is None:
            return None
        spread = Decimal(str(sigma))
        return self._anchor_up * (1 + spread), self._anchor_dn * (1 - spread)

    def _begin_day(self, bar: Bar, session_date: date) -> None:
        curve = self._curves.get(session_date.isoformat())
        if curve is None:
            self._state = DONE  # warmup or unknown session: no band, no trades
            return
        self._open = bar.open
        self._sigma = {int(k): float(v) for k, v in curve.get("sigma", {}).items()}
        prev_close = self._prev_close or _dec(curve.get("prev_close")) or bar.open
        # Gap adjustment per the source paper: the band hangs off whichever
        # side of the gap is wider, so an open far from yesterday's close
        # does not start the day already "abnormal".
        self._anchor_up = max(bar.open, prev_close)
        self._anchor_dn = min(bar.open, prev_close)
        self._state = ACTIVE
        if session_date in self._no_trade_dates:
            self._state = DONE

    # ------------------------------------------------------------------- OFI
    # Copied from sme.py (see module docstring for why it is not shared).

    def _proxy_delta(self, bar: Bar) -> Decimal:
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
        iso = session_date.isocalendar()
        week = f"{iso.year}-{iso.week}"
        if self._q_week == week and self._q_ofi is not None:
            return
        if len(self._ofi_sessions) < self._ofi_min_sessions:
            return
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
            return False
        reading = self._ofi(context)
        if reading is None:
            return False
        threshold = Decimal(str(self._q_ofi))
        return reading >= threshold if direction == "LONG" else reading <= -threshold

    # ----------------------------------------------------------------- exits

    def _stop_level(self, index: int) -> Decimal | None:
        """The structural stop for the CURRENT position at minute index."""
        bounds = self._bounds(index)
        vwap = self._vwap_pv / self._vwap_v if self._vwap_v else None
        if self._live_direction is Direction.LONG:
            level = bounds[1] if bounds else None  # LB_t
            if self._vwap_stop and vwap is not None:
                level = vwap if level is None else max(level, vwap)
            return level
        level = bounds[0] if bounds else None  # UB_t
        if self._vwap_stop and vwap is not None:
            level = vwap if level is None else min(level, vwap)
        return level

    def _exit_signal(self, bar: Bar, direction: Direction, why: str) -> Signal:
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            intent=SignalIntent.FLATTEN,
            direction=direction,
            quantity=self._quantity,
            reason=f"NAIM exit: {why}",
        )

    # ------------------------------------------------------------------ main

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        et = bar.close_time.astimezone(ET)
        et_time = et.time()
        session_date = et.date()

        if bar.timeframe == "5m":
            # Gate calibration samples come off 5m closes in the entry window.
            if self._ofi_mode != "off" and time(9, 31) <= et_time <= self._entry_end:
                reading = self._ofi(context)
                if reading is not None:
                    self._today_ofi_samples.append(abs(float(reading)))
            return None
        if bar.timeframe != "1m":
            return None
        if not (time(9, 30) < et_time <= time(16, 0)):
            return None  # overnight/late bars play no part in this strategy

        if self._state == IDLE:
            self._calibrate_gate(session_date)
            self._begin_day(bar, session_date)

        # VWAP and daily close accumulate on every RTH bar.
        self._vwap_pv += bar.close * bar.volume
        self._vwap_v += bar.volume
        self._today_close = bar.close
        index = self._minute_index(et.hour, et.minute)

        # Resolve a pending entry: did it actually fill?
        if self._state == PENDING:
            assert self._trades_at_entry is not None
            if context.trades_taken > self._trades_at_entry:
                self._entries_today += 1
                if context.position is not None:
                    self._state = IN_TRADE
                    self._live_direction = context.position.direction
                else:
                    self._state = ACTIVE  # filled and stopped within one bar
                    self._live_direction = None
            else:
                self._state = DONE  # the governor said no; do not argue
            self._trades_at_entry = None

        if self._state == IN_TRADE:
            signal = self._manage(bar, context, et_time, index, session_date)
            if signal is not None:
                return signal

        if self._state != ACTIVE:
            return None

        # Entry window and calendar.
        if et_time > self._entry_end:
            return None  # stay ACTIVE only to manage re-entries; none can fire
        if session_date in self._fomc_dates and et_time >= time(13, 45):
            return None
        if self._entries_today >= self._max_entries:
            self._state = DONE
            return None
        if context.is_warmup or context.position is not None:
            return None
        if self._trigger_mode == "30m" and et.minute % 30 != 0:
            return None

        bounds = self._bounds(index)
        if bounds is None:
            return None
        upper, lower = bounds
        if bar.close > upper:
            direction = "LONG"
        elif bar.close < lower:
            direction = "SHORT"
        else:
            return None
        if not self._gate_passes(context, direction):
            return None  # suppressed; the band can re-trigger on a later bar

        if direction == "LONG":
            fill = bar.close + self._tick
            stop = fill - self._r_max
            target = fill + self._far_target
            side = Direction.LONG
        else:
            fill = bar.close - self._tick
            stop = fill + self._r_max
            target = fill - self._far_target
            side = Direction.SHORT

        self._state = PENDING
        self._trades_at_entry = context.trades_taken
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            direction=side,
            entry_price=fill,
            stop_price=stop,
            target_price=target,
            quantity=self._quantity,
            reason=(
                f"NAIM {direction.lower()}: close {bar.close} outside noise band "
                f"({lower:.2f}..{upper:.2f}) at minute {index}"
            ),
            metadata={
                "strategy": self.name,
                "minute_index": index,
                "upper_band": str(upper),
                "lower_band": str(lower),
                "entry_number": self._entries_today + 1,
            },
        )

    def _manage(
        self, bar: Bar, context: Context, et_time: time, index: int, session_date: date
    ) -> Signal | None:
        position = context.position
        if position is None:
            # Catastrophic stop filled before this bar reached us.
            self._live_direction = None
            self._state = ACTIVE
            return None

        # Hard time exits.
        if et_time >= time(15, 55) or (
            session_date in self._fomc_dates and et_time >= time(13, 45)
        ):
            direction = position.direction
            self._live_direction = None
            self._state = DONE
            return self._exit_signal(bar, direction, "time")

        # Structural stop: price back inside the noise regime.
        level = self._stop_level(index)
        if level is None:
            return None
        long = position.direction is Direction.LONG
        if self._stop_mode == "close":
            violated = bar.close <= level if long else bar.close >= level
        else:
            violated = bar.low <= level if long else bar.high >= level
        if violated:
            direction = position.direction
            self._live_direction = None
            self._state = ACTIVE  # re-entry permitted if the band re-triggers
            return self._exit_signal(bar, direction, "structural stop (noise re-entry)")
        return None

    # ----------------------------------------------------------------- state

    def get_state(self) -> dict[str, Any]:
        return {
            "prev_close": None if self._prev_close is None else str(self._prev_close),
            "ofi_sessions": self._ofi_sessions,
            "q_ofi": self._q_ofi,
            "q_week": self._q_week,
            "state": self._state,
            "open": None if self._open is None else str(self._open),
            "anchor_up": None if self._anchor_up is None else str(self._anchor_up),
            "anchor_dn": None if self._anchor_dn is None else str(self._anchor_dn),
            "sigma": {str(k): v for k, v in self._sigma.items()},
            "vwap_pv": str(self._vwap_pv),
            "vwap_v": self._vwap_v,
            "entries_today": self._entries_today,
            "trades_at_entry": self._trades_at_entry,
            "live_direction": (
                None if self._live_direction is None else self._live_direction.value
            ),
            "today_close": None if self._today_close is None else str(self._today_close),
            "today_ofi_samples": self._today_ofi_samples,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self._prev_close = _dec(state.get("prev_close"))
        self._ofi_sessions = [
            [str(day), [float(s) for s in samples]]
            for day, samples in state.get("ofi_sessions", [])
        ]
        q = state.get("q_ofi")
        self._q_ofi = None if q is None else float(q)
        week = state.get("q_week")
        self._q_week = None if week is None else str(week)
        self._state = str(state.get("state", IDLE))
        self._open = _dec(state.get("open"))
        self._anchor_up = _dec(state.get("anchor_up"))
        self._anchor_dn = _dec(state.get("anchor_dn"))
        self._sigma = {int(k): float(v) for k, v in state.get("sigma", {}).items()}
        self._vwap_pv = Decimal(str(state.get("vwap_pv", "0")))
        self._vwap_v = int(state.get("vwap_v", 0))
        self._entries_today = int(state.get("entries_today", 0))
        raw_trades = state.get("trades_at_entry")
        self._trades_at_entry = None if raw_trades is None else int(raw_trades)
        raw_direction = state.get("live_direction")
        self._live_direction = None if raw_direction is None else Direction(str(raw_direction))
        self._today_close = _dec(state.get("today_close"))
        self._today_ofi_samples = [float(s) for s in state.get("today_ofi_samples", [])]
