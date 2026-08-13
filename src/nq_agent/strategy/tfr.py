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

EXIT_MODES = ("hf", "ri", "stack", "t13")


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
        t13_bars: int = 13,
        f4_confirm: bool = False,
        f3_veto: bool = False,
        catastrophic_frac: Decimal = Decimal("0.0035"),
        far_target_points: Decimal = Decimal("1000"),
        max_entries_per_day: int = 3,
        entry_end: time = time(15, 0),
        quantity: int = 1,
        tick_size: Decimal = Decimal("0.25"),
        fomc_dates: set[date] | None = None,
    ) -> None:
        if exit_mode not in EXIT_MODES:
            raise ValueError(f"exit_mode must be one of {EXIT_MODES}, got {exit_mode!r}")
        self._decisions = decisions or {}
        self._exit_mode = exit_mode
        self._q_entry_pct = str(q_entry_pct)
        self._q_hf_pct = str(q_hf_pct)
        self._p_arm = p_arm
        self._p_exit = p_exit
        self._vol_z_min = vol_z_min
        self._hysteresis = hysteresis_bars
        self._t13 = t13_bars
        self._f4_confirm = f4_confirm
        self._f3_veto = f3_veto
        self._cat_frac = catastrophic_frac
        self._far_target = far_target_points
        self._max_entries = max_entries_per_day
        self._entry_end = entry_end
        self._quantity = quantity
        self._tick = tick_size
        self._fomc_dates = fomc_dates or set()
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

    def _q(self, table_key: str) -> float | None:
        if self._day is None:
            return None
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
        if not (time(9, 30) < et_time <= time(16, 0)):
            return None
        index = (et.hour - 9) * 60 + et.minute - 30

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
        if et_time < time(9, 35) or et_time > self._entry_end:
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

        direction = self._entry_direction(record)
        if direction is None:
            return None
        # A reversal queued by _manage must match the fresh signal, else drop.
        if self._reverse_into is not None and self._reverse_into != direction:
            self._reverse_into = None
            return None
        self._reverse_into = None

        close = Decimal(str(record["close"]))
        if direction == "LONG":
            fill = close + self._tick
            stop = (fill * (1 - self._cat_frac)).quantize(self._tick)
            target = fill + self._far_target
            side = Direction.LONG
        else:
            fill = close - self._tick
            stop = (fill * (1 + self._cat_frac)).quantize(self._tick)
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

    def _entry_direction(self, record: dict[str, Any]) -> str | None:
        if record.get("regime") != "AF":
            return None
        t_af = record.get("t_af")
        if t_af is None or t_af < self._p_arm:
            return None
        z_vol = record.get("z_vol")
        if z_vol is None or z_vol < self._vol_z_min:
            return None
        q_entry = self._q("entry")
        f1 = record.get("f1_5")
        if q_entry is None or f1 is None or abs(f1) < q_entry or f1 == 0:
            return None
        direction = "LONG" if f1 > 0 else "SHORT"

        if self._f4_confirm:
            imbalance = record.get("large_imb") or 0.0
            if (imbalance > 0) != (f1 > 0):
                return None
        if self._f3_veto:
            # Absorption against the direction: heavy one-sided aggression
            # with price refusing to move (z_eff <= -1) opposing the entry.
            z_eff = record.get("z_eff")
            if z_eff is not None and z_eff <= -1.0:
                q_entry_val = self._q("entry")
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
        if et_time >= time(15, 55):
            return self._exit(bar, position.direction, "time 15:55", terminal=True)

        if record is None:
            return None
        long = position.direction is Direction.LONG

        if self._exit_mode in ("hf", "stack"):
            q_hf = self._q("hf")
            f1 = record.get("f1_5")
            if q_hf is not None and f1 is not None:
                hostile = f1 <= -q_hf if long else f1 >= q_hf
                if hostile:
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

    def _exit(
        self, bar: Bar, direction: Direction, why: str, terminal: bool = False
    ) -> Signal:
        self._live_direction = None
        self._bars_held = 0
        self._ri_regime_count = 0
        self._ri_prob_count = 0
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
        raw_reverse = state.get("reverse_into")
        self._reverse_into = None if raw_reverse is None else str(raw_reverse)
