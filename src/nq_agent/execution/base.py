from __future__ import annotations

from abc import ABC, abstractmethod

from nq_agent.models import OrderResult, Signal


class Executor(ABC):
    """One destination for one account.

    A config entry listing several accounts expands into several instances at
    wiring time, so per-account enable/disable is just `enabled` on an instance
    and the router's fan-out stays uniform.

    Signals carry absolute prices. Converting them to points, ticks or dollars
    is this layer's job, which is what keeps strategy logic broker-agnostic.
    """

    name: str
    account_id: str | None
    enabled: bool

    @abstractmethod
    async def execute(self, signal: Signal) -> OrderResult:
        """Send the signal. Must not raise — return a failed OrderResult instead."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Cheap liveness probe, called once at startup."""


class NotifyExecutor(Executor):
    """An executor that also carries out-of-band alerts.

    The router runs notify executors first, because the human reading them is
    the slow leg, and uses `alert` to report partial fan-out failures.
    """

    @abstractmethod
    async def alert(self, message: str) -> None:
        """Push an operational message that is not itself a trade."""
