from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from nq_agent.context import Context
from nq_agent.models import Bar, Signal


class Strategy(ABC):
    """The one interface a real strategy implements.

    Every method is sync on purpose. The strategy does no network, no file
    access and no wall-clock reads — it takes time from the context. Sync
    signatures mean a strategy structurally cannot await a network call, so
    purity is enforced by the type system rather than by discipline.
    """

    name: str
    required_timeframes: list[str]

    @abstractmethod
    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        """Called once per closed bar. Return a Signal to trade, or None."""

    @abstractmethod
    def on_session_start(self, session_date: date) -> None:
        """Reset per-session state."""

    @abstractmethod
    def on_session_end(self, session_date: date) -> None:
        """Tear down per-session state."""

    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        """Serialisable internal state, for crash recovery."""

    @abstractmethod
    def restore_state(self, state: dict[str, Any]) -> None:
        """Rebuild internal state from get_state output."""
