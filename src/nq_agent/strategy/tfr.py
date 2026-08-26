"""Tick-Flow Regime (TFR) v1 -- spec v1.0.

State-based entries AND exits; no price-level triggers anywhere. A GMM
regime model (fit walk-forward offline) plus tick-flow features decide when
the market is in an Active-Flow state worth riding; entries follow the
direction of the canonical flow signal (F1_5) when it clears its trailing
percentile; exits are thesis-death only -- hostile flow (V-HF), regime
invalidation with hysteresis (V-RI), both (V-STACK), or the pre-registered
time baseline (V-T13) -- never profit protection. Backstops on every
variant: a fraction-scaled catastrophic stop (0.35% of entry price;
NAIM's fixed-point decay lesson locked) and the hard 15:55 flatten.

The strategy runtime does NO statistics. Per-session decision files
(scripts/precompute_flow.py -> scripts/fit_regimes.py) carry every
feature, regime label, transition probability, percentile table and
model-health cut, all computed walk-forward with zero lookahead. This
module reads them and applies logic -- the same split NAIM used for its
noise curves, and the shadow harness will compute the identical values in
real time from the live tick feed.

Deviations from spec v1.0 in this engine, each deliberate:

- Entry fills model at the 5m decision bar's close + 1 tick adverse (the
  spec says next 1m open + 1 tick; on this fixture set those differ by
  sub-tick amounts and the engine fills at signal time).
- Stop-and-reverse is two steps (flatten on the signal bar, enter on the
  next bar if the signal persists): one signal per bar is an engine
  invariant. Both steps count toward the entry cap, per spec.
- The catastrophic stop needs a target for the Signal contract; a far
  target (1000 points) stands in for "none".
- Sizing is a constant `quantity` (default 1). Per-contract economics,
  reported NQ-equivalent, exactly as SME/NAIM.
- FOMC handling per spec section 5 is an ENTRY block after 13:00; the spec
  prescribes no FOMC flatten for open positions and none is added.
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

IDLE = "idle"
ACTIVE = "active"
PENDING = "pending"
IN_TRADE = "in_trade"
DONE = "done"

# v1.0 modes (regime-based) kept for the record; v1.1 adds the flow-native
# family: "fd" (flow decay) and "fstack" (fd + hf, whichever fires first).
# "bracket" (SSX-V3): no thesis exits and no stop-and-reverse -- the
# server-side target/stop pair placed at entry is the whole exit model,
# with only the session flatten (and an optional bar-count backstop) on top.
EXIT_MODES = ("hf", "ri", "stack", "t13", "fd", "fstack", "bracket")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class TickFlowRegime(Strategy):
    name = "tfr"
    required_timeframes = ["1m", "5m"]

    def __init__(
        self,
        *,
        decisions: dict[str, dict[str, Any]] | None = None,
        exit_mode: str = "stack",
        q_entry_pct: int = 70,
        q_hf_pct: int = 70,
        p_arm: float = 0.60,
        p_exit: float = 0.40,
        vol_z_min: float = 0.5,
        hysteresis_bars: int = 2,
        fd_hysteresis_bars: int = 2,
        t13_bars: int = 13,
        f4_confirm: bool = False,
        f3_veto: bool = False,
        catastrophic_frac: Decimal = Decimal("0.0035"),
        far_target_points: Decimal = Decimal("1000"),
        target_frac: Decimal | None = None,
        bracket_backstop_bars: int | None = None,
        max_entries_per_day: int = 3,
        entry_end: time = time(15, 0),
        quantity: int = 1,
        tick_size: Decimal = Decimal("0.25"),
        fomc_dates: set[date] | None = None,
        regime_required: bool = True,
        session_mode: str = "rth",
    ) -> None:
        if exit_mode not in EXIT_MODES:
            raise ValueError(f"exit_mode must be one of {EXIT_MODES}, got {exit_mode!r}")
        if session_mode not in ("rth", "all"):
            raise ValueError(f"session_mode must be 'rth' or 'all', got {session_mode!r}")
        # `if ... is None`, NOT `or`: the live wiring hands over a SHARED,
        # initially-EMPTY book that the flow engine fills as ticks arrive.
        # `decisions or {}` would replace that empty-but-shared dict with a
        # private one and the strategy would stare at nothing all day --
        # which is exactly what happened on the first live session.
        self._decisions = decisions if decisions is not None else {}
        self._exit_mode = exit_mode
        self._q_entry_pct = str(q_entry_pct)
        self._q_hf_pct = str(q_hf_pct)
        self._p_arm = p_arm
        self._p_exit = p_exit
        self._vol_z_min = vol_z_min
        self._hysteresis = hysteresis_bars
        self._fd_hysteresis = fd_hysteresis_bars
        self._t13 = t13_bars
        self._f4_confirm = f4_confirm
        self._f3_veto = f3_veto
        self._cat_frac = catastrophic_frac
        self._far_target = far_target_points
        # SSX-V3: a real (small) profit target, recorded as a FRACTION of
        # price at spec-lock and rebased on every entry, exactly like the
        # catastrophic stop -- NAIM's fixed-point erosion is not repeated.
        # None keeps the far stand-in target ("none") for every other mode.
        self._target_frac = target_frac
        self._bracket_backstop = bracket_backstop_bars
        self._max_entries = max_entries_per_day
        self._entry_end = entry_end
        self._quantity = quantity
        self._tick = tick_size
        self._fomc_dates = fomc_dates or set()
        # False = the spec 10.3 control: flow-threshold entries with the
        # regime layer bypassed entirely, so the layer's contribution is
        # measured rather than assumed.
        self._regime_required = regime_required
        # "all": trade every hour except the 16:30-18:00 blackout and the
        # engine's midnight session boundary, with entry buffers sized so a
        # full 13-bar hold cannot land inside either. "rth": the declared
        # shadow behaviour, unchanged.
        self._session_mode = session_mode
        self._reset_day()

    # ------------------------------------------------------------------ day

    def _reset_day(self) -> None:
        self._state = IDLE
        self._day: dict[str, Any] | None = None
        self._entries_today = 0
        self._trades_at_entry: int | None = None
        self._live_direction: Direction | None = None
        self._bars_held = 0
        self._ri_regime_count = 0
        self._ri_prob_count = 0
        self._fd_count = 0
        self._stale_bars = 0
        self._reverse_into: str | None = None

    def on_session_start(self, session_date: date) -> None:
        self._reset_day()

    def on_session_end(self, session_date: date) -> None:
        return None

    # ------------------------------------------------------------------ data

    def _bar_record(self, index: int) -> dict[str, Any] | None:
        if self._day is None:
            return None
        record = self._day.get("bars", {}).get(str(index))
        return record if isinstance(record, dict) else None

    def _q(self, table_key: str, record: dict[str, Any] | None = None) -> float | None:
        if self._day is None:
            return None
        blocks = self._day.get("q_f1_blocks")
        if blocks is not None:
            block = (record or {}).get("block")
            table = blocks.get(block) if block else None
        else:
            table = self._day.get("q_f1")
        if not table:
            return None
        key = self._q_entry_pct if table_key == "entry" else self._q_hf_pct
        value = table.get(key)
        return None if value is None else float(value)

    # ------------------------------------------------------------------ main

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe != "5m":
            return None
        et = bar.close_time.astimezone(ET)
        et_time = et.time()
        session_date = et.date()
        if self._session_mode == "rth":
            if not (time(9, 30) < et_time <= time(16, 0)):
                return None
            index = (et.hour - 9) * 60 + et.minute - 30
        else:
            index = et.hour * 60 + et.minute
            if index == 0:
                index = 1440  # a bar closing exactly at midnight ends the day

        # _day is derived, not persisted: derive it on the first bar seen,
        # including the first bar after a mid-session resume, so a restored
        # IN_TRADE keeps managing its position instead of being stranded.
        if self._day is None and self._state != DONE:
            self._day = self._decisions.get(session_date.isoformat())
            if self._day is None or self._day.get("model") is None:
                self._state = DONE  # no decisions or unfitted model: stand down
            elif self._state == IDLE:
                self._state = ACTIVE

        record = self._bar_record(index)

        # Data-health halt (v1.1): feature records missing for more than two
        # consecutive decision bars means the feed and the features have
        # diverged -- stand the session down rather than trade blind.
        if self._day is not None and self._state in (ACTIVE, PENDING, IN_TRADE):
            self._stale_bars = self._stale_bars + 1 if record is None else 0
            if self._stale_bars > 2:
                if self._state == IN_TRADE and context.position is not None:
                    held = context.position.direction
                    return self._exit(bar, held, "data health halt", terminal=True)
                self._state = DONE
                return None

        # Model health (spec section 7): a bar off the fitted model's map
        # stands the session down. Never trade a model outside its support.
        cut = None if self._day is None else self._day.get("mahal_cut")
        if (
            record is not None
            and cut is not None
            and record.get("mahal") is not None
            and record["mahal"] > cut
            and self._state in (ACTIVE, PENDING)
        ):
            self._state = DONE
            return None

        # Resolve a pending entry.
        if self._state == PENDING:
            assert self._trades_at_entry is not None
            if context.trades_taken > self._trades_at_entry:
                self._entries_today += 1
                if context.position is not None:
                    self._state = IN_TRADE
                    self._live_direction = context.position.direction
                    self._bars_held = 0
                    self._ri_regime_count = 0
                    self._ri_prob_count = 0
                else:
                    self._state = ACTIVE  # same-bar catastrophic stop
                    self._live_direction = None
            else:
                self._state = DONE  # governor refusal ends the day
            self._trades_at_entry = None

        if self._state == IN_TRADE:
            signal = self._manage(bar, context, et_time, record)
            if signal is not None:
                return signal

        if self._state != ACTIVE:
            return None

        # Entry window, calendar, caps.
        if self._session_mode == "rth":
            if et_time < time(9, 35) or et_time > self._entry_end:
                return None
        else:
            # Buffers sized to the 13-bar (65 min) hold: no entry whose hold
            # would cross the 16:30 blackout or the midnight boundary.
            if time(15, 20) <= et_time < time(18, 5):
                return None
            if et_time >= time(22, 50) or et_time < time(0, 5):
                return None
        if session_date in self._fomc_dates and et_time > time(13, 0):
            return None
        if self._entries_today >= self._max_entries:
            self._state = DONE
            return None
        if context.is_warmup or context.position is not None:
            return None
        if record is None:
            return None

        # A reversal queued by _manage is good for exactly one evaluated bar:
        # it must match THIS bar's fresh signal or be forgotten. Left armed
        # past a bar where the signal faded, a stale intent would silently
        # drop an unrelated signal much later in the day.
        reverse_into, self._reverse_into = self._reverse_into, None
        direction = self._entry_direction(record)
        if direction is None:
            return None
        if reverse_into is not None and reverse_into != direction:
            return None

        close = Decimal(str(record["close"]))
        if direction == "LONG":
            fill = close + self._tick
            stop = self._round_to_tick(fill * (1 - self._cat_frac))
            if self._target_frac is not None:
                target = self._round_to_tick(fill * (1 + self._target_frac))
            else:
                target = fill + self._far_target
            side = Direction.LONG
        else:
            fill = close - self._tick
            stop = self._round_to_tick(fill * (1 + self._cat_frac))
            if self._target_frac is not None:
                target = self._round_to_tick(fill * (1 - self._target_frac))
            else:
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
                f"TFR {direction.lower()}: AF regime armed, "
                f"F1_5={record['f1_5']} beyond Q{self._q_entry_pct}"
            ),
            metadata={
                "strategy": self.name,
                "minute_index": index,
                "regime": record.get("regime"),
                "t_af": record.get("t_af"),
                "f1_5": record.get("f1_5"),
                "entry_number": self._entries_today + 1,
            },
        )

    # ----------------------------------------------------------------- entry

    def _round_to_tick(self, value: Decimal) -> Decimal:
        """Round to the instrument's tick grid.

        NOT `value.quantize(self._tick)`: quantize matches its argument's
        EXPONENT, so quantize(Decimal("0.25")) rounds to hundredths and
        produces off-grid prices like ...30.07 -- accepted silently by the
        dryrun executor, rejected (or re-rounded differently) by a broker.
        """
        return (value / self._tick).quantize(Decimal("1")) * self._tick

    def _entry_direction(self, record: dict[str, Any]) -> str | None:
        if self._regime_required:
            if record.get("regime") != "AF":
                return None
            t_af = record.get("t_af")
            if t_af is None or t_af < self._p_arm:
                return None
        z_vol = record.get("z_vol")
        if z_vol is None or z_vol < self._vol_z_min:
            return None
        q_entry = self._q("entry", record)
        f1 = record.get("f1_5")
        if q_entry is None or f1 is None or abs(f1) < q_entry or f1 == 0:
            return None
        direction = "LONG" if f1 > 0 else "SHORT"

        if self._f4_confirm:
            # Missing or exactly-zero large-trade imbalance cannot CONFIRM
            # either direction -- no entry. (The old `or 0.0` coercion made
            # an absent field veto every long while passing every short.)
            imbalance = record.get("large_imb")
            if imbalance is None or imbalance == 0 or (imbalance > 0) != (f1 > 0):
                return None
        if self._f3_veto:
            # Absorption against the direction: heavy one-sided aggression
            # with price refusing to move (z_eff <= -1) opposing the entry.
            z_eff = record.get("z_eff")
            if z_eff is not None and z_eff <= -1.0:
                q_entry_val = self._q("entry", record)
                f1_val = record.get("f1_5") or 0.0
                if q_entry_val is not None and abs(f1_val) >= q_entry_val:
                    # flow this heavy being absorbed vetoes trading WITH it
                    return None
        return direction

    # ------------------------------------------------------------------ exit

    def _manage(
        self, bar: Bar, context: Context, et_time: time, record: dict[str, Any] | None
    ) -> Signal | None:
        position = context.position
        if position is None:
            # Catastrophic stop filled before this bar reached us.
            self._live_direction = None
            self._state = ACTIVE
            return None

        self._bars_held += 1
        if context.is_warmup:
            # The engine suppresses strategy signals during the restart
            # catch-up window (journalled as signal_suppressed_backfill). An
            # exit emitted here would be dropped AFTER _exit had already
            # reset this state machine to ACTIVE/DONE -- leaving the still-
            # open position unmanaged for the rest of the day. Keep counting
            # held bars; every exit condition is re-evaluated on the first
            # live bar, where the signal can actually reach the executor.
            return None
        if self._session_mode == "rth":
            if et_time >= time(15, 55):
                return self._exit(bar, position.direction, "time 15:55", terminal=True)
        else:
            if time(16, 25) <= et_time < time(18, 0):
                # Pre-blackout flatten; the evening session may re-enter.
                return self._exit(bar, position.direction, "blackout flatten")
            if et_time >= time(23, 45):
                return self._exit(bar, position.direction, "midnight flatten", terminal=True)

        if self._exit_mode == "bracket":
            # SSX-V3: the bracket IS the exit model. No thesis exits, no
            # stop-and-reverse -- an opposite signal while a position is
            # open is ignored, not traded. Only the optional bar-count
            # backstop runs here; target and stop fill server-side.
            if (
                self._bracket_backstop is not None
                and self._bars_held >= self._bracket_backstop
            ):
                return self._exit(
                    bar,
                    position.direction,
                    f"time backstop {self._bracket_backstop} bars",
                )
            return None

        if record is None:
            return None
        long = position.direction is Direction.LONG

        if self._exit_mode in ("hf", "stack") and self._hostile_flow(record, long):
            return self._exit(bar, position.direction, "hostile flow")

        if self._exit_mode in ("fd", "fstack"):
            # Flow decay: F1_5 through zero AGAINST the position for
            # `fd_hysteresis` consecutive bars. Thesis-symmetric: entered on
            # extreme flow in this direction, out when flow no longer points
            # this way at all.
            f1_now = record.get("f1_5")
            against = f1_now is not None and (f1_now < 0 if long else f1_now > 0)
            self._fd_count = self._fd_count + 1 if against else 0
            if self._fd_count >= self._fd_hysteresis:
                return self._exit(bar, position.direction, "flow decay")

        # fstack checks flow decay first (above), then hostile flow -- the
        # order decides which reason a both-fire bar records.
        if self._exit_mode == "fstack" and self._hostile_flow(record, long):
            return self._exit(bar, position.direction, "hostile flow")

        if self._exit_mode in ("ri", "stack"):
            self._ri_regime_count = (
                self._ri_regime_count + 1 if record.get("regime") != "AF" else 0
            )
            t_af = record.get("t_af")
            self._ri_prob_count = (
                self._ri_prob_count + 1
                if t_af is not None and t_af < self._p_exit
                else 0
            )
            if (
                self._ri_regime_count >= self._hysteresis
                or self._ri_prob_count >= self._hysteresis
            ):
                which = (
                    "regime left AF"
                    if self._ri_regime_count >= self._hysteresis
                    else "AF retention prob collapsed"
                )
                return self._exit(bar, position.direction, f"regime invalidation: {which}")

        if self._exit_mode == "t13" and self._bars_held >= self._t13:
            return self._exit(bar, position.direction, f"time baseline {self._t13} bars")

        # Stop-and-reverse: a fresh, fully qualified opposite signal.
        opposite = self._entry_direction(record)
        held = "LONG" if long else "SHORT"
        if opposite is not None and opposite != held:
            self._reverse_into = opposite
            return self._exit(bar, position.direction, "reverse: opposite signal")

        return None

    def _hostile_flow(self, record: dict[str, Any], long: bool) -> bool:
        """V-HF: flow beyond the hostile percentile AGAINST the position."""
        q_hf = self._q("hf", record)
        f1 = record.get("f1_5")
        if q_hf is None or f1 is None:
            return False
        return bool(f1 <= -q_hf if long else f1 >= q_hf)

    def _exit(
        self, bar: Bar, direction: Direction, why: str, terminal: bool = False
    ) -> Signal:
        self._live_direction = None
        self._bars_held = 0
        self._ri_regime_count = 0
        self._ri_prob_count = 0
        self._fd_count = 0
        self._state = DONE if terminal else ACTIVE
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            intent=SignalIntent.FLATTEN,
            direction=direction,
            quantity=self._quantity,
            reason=f"TFR exit: {why}",
        )

    # ----------------------------------------------------------------- state

    def get_state(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "entries_today": self._entries_today,
            "trades_at_entry": self._trades_at_entry,
            "live_direction": (
                None if self._live_direction is None else self._live_direction.value
            ),
            "bars_held": self._bars_held,
            "ri_regime_count": self._ri_regime_count,
            "ri_prob_count": self._ri_prob_count,
            "fd_count": self._fd_count,
            "stale_bars": self._stale_bars,
            "reverse_into": self._reverse_into,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self._state = str(state.get("state", IDLE))
        self._entries_today = int(state.get("entries_today", 0))
        raw = state.get("trades_at_entry")
        self._trades_at_entry = None if raw is None else int(raw)
        raw_direction = state.get("live_direction")
        self._live_direction = None if raw_direction is None else Direction(str(raw_direction))
        self._bars_held = int(state.get("bars_held", 0))
        self._ri_regime_count = int(state.get("ri_regime_count", 0))
        self._ri_prob_count = int(state.get("ri_prob_count", 0))
        self._fd_count = int(state.get("fd_count", 0))
        self._stale_bars = int(state.get("stale_bars", 0))
        raw_reverse = state.get("reverse_into")
        self._reverse_into = None if raw_reverse is None else str(raw_reverse)
