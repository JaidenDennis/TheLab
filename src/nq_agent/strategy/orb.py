"""Opening-range breakout — a REFERENCE implementation, not a validated edge.

This exists so the backtester has something with real per-session state to
chew on, and so there is a worked example of a stateful strategy that survives
a crash. It is a textbook pattern, chosen because it is easy to reason about,
not because it has been shown to make money. No edge has been demonstrated for
it on this instrument or any other. Backtest it yourself before it goes
anywhere near an account.

The rules:

  - The opening range is the high and low of the first `range_minutes` of the
    session.
  - After the range closes, the first 1m bar to CLOSE outside it opens a
    position in that direction: long above the high, short below the low.
  - The stop goes on the opposite side of the range; the target is
    `reward_multiple` times the risk.
  - One entry per session, first signal only.

Two things worth copying from here into a real strategy:

1. `restore_state` coerces every value defensively. State round-trips through
   JSON, so the Decimals and datetimes it stores come back as strings.
2. `on_session_start` clears everything, and the engine's resume path adopts a
   session without calling it (see SessionManager) precisely so that clearing
   cannot undo a restore.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Signal
from nq_agent.strategy.base import Strategy


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class OpeningRangeBreakout(Strategy):
    name = "orb"
    required_timeframes = ["1m"]

    def __init__(
        self,
        range_minutes: int = 15,
        reward_multiple: Decimal = Decimal("2"),
        quantity: int = 1,
        session_open: time = time(13, 30),  # 09:30 New York, in UTC
    ) -> None:
        if range_minutes < 1:
            raise ValueError(f"range_minutes must be at least 1, got {range_minutes}")
        if reward_multiple <= 0:
            raise ValueError(f"reward_multiple must be positive, got {reward_multiple}")
        if quantity < 1:
            raise ValueError(f"quantity must be at least 1, got {quantity}")
        self._range_minutes = range_minutes
        self._reward = reward_multiple
        self._quantity = quantity
        self._session_open = session_open
        self._high: Decimal | None = None
        self._low: Decimal | None = None
        self._range_end: datetime | None = None
        self._fired = False

    def _build_range(self, bar: Bar) -> None:
        self._high = bar.high if self._high is None else max(self._high, bar.high)
        self._low = bar.low if self._low is None else min(self._low, bar.low)

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe != "1m" or self._fired:
            return None

        if self._range_end is None:
            # Anchored on the first bar seen rather than on a clock read: the
            # strategy has no wall clock by design, and a session that opens
            # late (or a fixture that starts mid-session) must still produce a
            # range of the length asked for.
            self._range_end = bar.open_time.replace(
                hour=self._session_open.hour,
                minute=self._session_open.minute,
                second=0,
                microsecond=0,
            ) + (bar.close_time - bar.open_time) * self._range_minutes

        if bar.close_time <= self._range_end:
            self._build_range(bar)
            return None

        if self._high is None or self._low is None or self._high <= self._low:
            return None

        if bar.close > self._high:
            direction = Direction.LONG
            entry, stop = bar.close, self._low
        elif bar.close < self._low:
            direction = Direction.SHORT
            entry, stop = bar.close, self._high
        else:
            return None

        risk = abs(entry - stop)
        if risk <= 0:
            return None
        reward = risk * self._reward
        target = entry + reward if direction is Direction.LONG else entry - reward

        self._fired = True
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            direction=direction,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            quantity=self._quantity,
            reason=(
                f"{self._range_minutes}m opening range "
                f"{self._low}-{self._high} broken to the "
                f"{'upside' if direction is Direction.LONG else 'downside'}"
            ),
        )

    def on_session_start(self, session_date: date) -> None:
        self._high = None
        self._low = None
        self._range_end = None
        self._fired = False

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {
            "high": None if self._high is None else str(self._high),
            "low": None if self._low is None else str(self._low),
            "range_end": None if self._range_end is None else self._range_end.isoformat(),
            "fired": self._fired,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        # Coerced, not assigned: everything here came back through JSON, so
        # the Decimals are strings and the datetime is an ISO string. A
        # strategy that assumes type fidelity across a restart compares a str
        # to a Decimal on the first bar after a crash and raises.
        self._high = _as_decimal(state.get("high"))
        self._low = _as_decimal(state.get("low"))
        self._range_end = _as_datetime(state.get("range_end"))
        self._fired = bool(state.get("fired", False))
