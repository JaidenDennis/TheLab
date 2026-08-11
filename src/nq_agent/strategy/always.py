from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.strategy.base import Strategy


class AlwaysStrategy(Strategy):
    """Fires one long entry on the first 1m bar of each session.

    Exists to prove the pipeline end to end. It is not a trading idea.
    """

    name = "always"
    required_timeframes = ["1m"]
    base_timeframe = "1m"

    def __init__(
        self,
        stop_offset: Decimal = Decimal("10"),
        target_offset: Decimal = Decimal("20"),
    ) -> None:
        self._stop_offset = stop_offset
        self._target_offset = target_offset
        self._fired = False

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe != self.base_timeframe or self._fired:
            return None

        self._fired = True
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            intent=SignalIntent.ENTRY,
            direction=Direction.LONG,
            entry_price=bar.close,
            stop_price=bar.close - self._stop_offset,
            target_price=bar.close + self._target_offset,
            quantity=1,
            reason="always strategy: first bar of session",
            metadata={"bar_open_time": bar.open_time.isoformat()},
        )

    def on_session_start(self, session_date: date) -> None:
        # Clears what restore_state sets. On a resume, call this first.
        self._fired = False

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {"fired": self._fired}

    def restore_state(self, state: dict[str, Any]) -> None:
        self._fired = bool(state.get("fired", False))
