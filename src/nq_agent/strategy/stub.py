from __future__ import annotations

from datetime import date
from typing import Any

from nq_agent.context import Context
from nq_agent.models import Bar, Signal
from nq_agent.strategy.base import Strategy


class StubStrategy(Strategy):
    """Never fires. Proves the pipeline runs a full session without trading."""

    name = "stub"
    required_timeframes = ["1m", "5m"]

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        return None

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {}

    def restore_state(self, state: dict[str, Any]) -> None:
        return None
