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
        """Reset per-session state.

        Implementations may clear everything `restore_state` would set, so on a
        resume this must run before `restore_state`, not after.
        """

    @abstractmethod
    def on_session_end(self, session_date: date) -> None:
        """Tear down per-session state."""

    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        """Serialisable internal state, for crash recovery."""

    @abstractmethod
    def restore_state(self, state: dict[str, Any]) -> None:
        """Rebuild internal state from get_state output.

        Ordering matters when resuming a crashed session. `on_session_start` may
        unconditionally clear the very state this restores, so a caller resuming
        persisted state must call `restore_state` *after* `on_session_start` for
        that session. Restoring first silently discards the state.
        """
