"""Absorption Flow Reversal (AFR) v1 -- spec v1.0, program 7.

Mean reversion triggered by FLOW STATE, never price: heavy one-sided
aggression (|F1_5| >= Q_abs) that produces anomalously little displacement
(z_eff <= -E_thresh) is being absorbed; enter AGAINST the aggressor side.
Exits are MR-shaped and bounded: a time ladder or flow-normalization,
always under the non-optional continuation stop (the original aggressor
side reasserting past Q_abs is thesis death), the 0.35% catastrophic
stop, and the 15:55 flatten.

`absorption_required=False` is the pre-registered naive-fade control:
identical entries without the absorption condition. External evidence
says the naive fade loses; the mechanism is validated only by the
predicted split (naive <= $0, AFR > 0).

Consumes the same walk-forward decision files as TFR (f1_5, z_eff and
the q_f1 table are already in them); the shared-book contract from
tfr.py applies unchanged. Same engine deviations as the house standard:
fills at decision-bar close +1 tick adverse, far target stands in for
"none", constant quantity, one signal per bar (reversals not needed --
AFR never reverses, it exits).
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

EXIT_MODES = ("t3", "t6", "t13", "norm")
TIME_BARS = {"t3": 3, "t6": 6, "t13": 13}


def _round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).quantize(Decimal("1")) * tick


class AbsorptionFlowReversal(Strategy):
    name = "afr"
    required_timeframes = ["1m", "5m"]

    def __init__(
        self,
        *,
        decisions: dict[str, dict[str, Any]] | None = None,
        exit_mode: str = "t6",
        q_abs_pct: int = 70,
        e_thresh: float = 1.0,
        absorption_required: bool = True,
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
        # Shared-book contract: `is None`, never `or` (see tfr.py).
        self._decisions = decisions if decisions is not None else {}
        self._exit_mode = exit_mode
        self._q_pct = str(q_abs_pct)
        self._e_thresh = e_thresh
        self._absorption_required = absorption_required
        self._cat_frac = catastrophic_frac
        self._far_target = far_target_points
        self._max_entries = max_entries_per_day
        self._entry_end = entry_end
        self._quantity = quantity
        self._tick = tick_size
        self._fomc_dates = fomc_dates or set()
        self._reset_day()

    def _reset_day(self) -> None:
        self._state = IDLE
        self._day: dict[str, Any] | None = None
        self._entries_today = 0
        self._trades_at_entry: int | None = None
        self._bars_held = 0
        self._aggressor_sign = 0  # +1 buy aggression faded, -1 sell

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

    def _q_abs(self) -> float | None:
        if self._day is None:
            return None
        table = self._day.get("q_f1")
        if not table:
            return None
        value = table.get(self._q_pct)
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

        if self._day is None and self._state != DONE:
            self._day = self._decisions.get(session_date.isoformat())
            if self._day is None or self._day.get("model") is None:
                self._state = DONE
            elif self._state == IDLE:
                self._state = ACTIVE

        record = self._bar_record(index)

        # Resolve a pending entry.
        if self._state == PENDING:
            assert self._trades_at_entry is not None
            if context.trades_taken > self._trades_at_entry:
                self._entries_today += 1
                if context.position is not None:
                    self._state = IN_TRADE
                    self._bars_held = 0
                else:
                    self._state = ACTIVE  # same-bar catastrophic stop
            else:
                self._state = DONE  # governor refusal ends the day
            self._trades_at_entry = None

        if self._state == IN_TRADE:
            signal = self._manage(bar, context, et_time, record)
            if signal is not None:
                return signal

        if self._state != ACTIVE:
            return None
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

        q_abs = self._q_abs()
        f1 = record.get("f1_5")
        z_eff = record.get("z_eff")
        if q_abs is None or f1 is None or f1 == 0 or abs(f1) < q_abs:
            return None
        if self._absorption_required and (z_eff is None or z_eff > -self._e_thresh):
            return None

        # Enter AGAINST the aggressor side.
        self._aggressor_sign = 1 if f1 > 0 else -1
        close = Decimal(str(record["close"]))
        if self._aggressor_sign > 0:  # buy aggression absorbed -> fade short
            fill = close - self._tick
            stop = _round_to_tick(fill * (1 + self._cat_frac), self._tick)
            target = fill - self._far_target
            side = Direction.SHORT
        else:
            fill = close + self._tick
            stop = _round_to_tick(fill * (1 - self._cat_frac), self._tick)
            target = fill + self._far_target
            side = Direction.LONG

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
                f"AFR fade: |F1_5|={abs(f1):.4f} >= Q{self._q_pct}, "
                f"z_eff={z_eff} (absorption={'on' if self._absorption_required else 'OFF/control'})"
            ),
            metadata={
                "strategy": self.name,
                "minute_index": index,
                "f1_5": f1,
                "z_eff": z_eff,
                "aggressor_sign": self._aggressor_sign,
                "entry_number": self._entries_today + 1,
            },
        )

    def _manage(
        self, bar: Bar, context: Context, et_time: time, record: dict[str, Any] | None
    ) -> Signal | None:
        position = context.position
        if position is None:
            self._state = ACTIVE  # catastrophic stop filled before this bar
            return None
        if context.is_warmup:
            self._bars_held += 1
            return None

        self._bars_held += 1
        if et_time >= time(15, 55):
            return self._exit(bar, position.direction, "time 15:55", terminal=True)

        if record is not None:
            # A-CONT, non-optional under every variant: the original
            # aggressor side reasserting past Q_abs means absorption failed.
            q_abs = self._q_abs()
            f1 = record.get("f1_5")
            if q_abs is not None and f1 is not None:
                reasserted = (
                    f1 >= q_abs if self._aggressor_sign > 0 else f1 <= -q_abs
                )
                if reasserted:
                    return self._exit(bar, position.direction, "continuation: absorption failed")

            if self._exit_mode == "norm":
                z_eff = record.get("z_eff")
                if z_eff is not None and z_eff >= 0:
                    return self._exit(bar, position.direction, "flow normalized")

        if self._exit_mode in TIME_BARS and self._bars_held >= TIME_BARS[self._exit_mode]:
            return self._exit(bar, position.direction, f"time {self._exit_mode}")

        return None

    def _exit(
        self, bar: Bar, direction: Direction, why: str, terminal: bool = False
    ) -> Signal:
        self._bars_held = 0
        self._aggressor_sign = 0
        self._state = DONE if terminal else ACTIVE
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            intent=SignalIntent.FLATTEN,
            direction=direction,
            quantity=self._quantity,
            reason=f"AFR exit: {why}",
        )

    # ----------------------------------------------------------------- state

    def get_state(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "entries_today": self._entries_today,
            "trades_at_entry": self._trades_at_entry,
            "bars_held": self._bars_held,
            "aggressor_sign": self._aggressor_sign,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self._state = str(state.get("state", IDLE))
        self._entries_today = int(state.get("entries_today", 0))
        raw = state.get("trades_at_entry")
        self._trades_at_entry = None if raw is None else int(raw)
        self._bars_held = int(state.get("bars_held", 0))
        self._aggressor_sign = int(state.get("aggressor_sign", 0))
