from __future__ import annotations

import logging

from nq_agent.execution.base import Executor, NotifyExecutor
from nq_agent.models import OrderOutcome, OrderResult, Signal

logger = logging.getLogger(__name__)


def _describe(signal: Signal) -> dict[str, str]:
    described = {
        "signal_id": signal.id,
        "intent": signal.intent.value,
        "direction": signal.direction.value,
        "symbol": signal.symbol,
        "quantity": str(signal.quantity),
    }
    for field in ("entry_price", "stop_price", "target_price"):
        value = getattr(signal, field)
        if value is not None:
            described[field] = str(value)
    return described


def _dry_run_result(
    name: str, account_id: str | None, signal: Signal, verb: str
) -> OrderResult:
    """Build the OrderResult both dry-run classes return.

    Shared deliberately: the real webhook executor and the real notifier arrive
    as new sibling classes, not as these two growing apart, so there is nothing
    here that has to be pulled back apart later.
    """
    payload = _describe(signal)
    logger.info("dry run %s %s %s", verb, name, payload)
    return OrderResult(
        signal_id=signal.id,
        executor_name=name,
        outcome=OrderOutcome.FILLED,
        account_id=account_id,
        latency_ms=0,
        raw_response=payload,
    )


class DryRunExecutor(Executor):
    """Logs instead of trading. Produces the same OrderResult shape as the real thing."""

    def __init__(self, name: str, account_id: str | None = None, enabled: bool = True) -> None:
        # `is not None`, not truthiness: an empty account_id must not quietly
        # take the unsuffixed branch while self.account_id still stores "".
        self.name = f"{name}:{account_id}" if account_id is not None else name
        self.account_id = account_id
        self.enabled = enabled
        self.sent: list[Signal] = []
        # Recorded for the same reason `sent` is: this class is the inspectable
        # stand-in for a real executor, and "was I released?" is exactly what a
        # real one holding a network session needs answered.
        self.closed = 0

    async def execute(self, signal: Signal) -> OrderResult:
        self.sent.append(signal)
        return _dry_run_result(self.name, self.account_id, signal, "execute")

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed += 1


class DryRunNotifier(NotifyExecutor):
    """Stands in for ntfy or Pushover until one is chosen."""

    def __init__(self, name: str, enabled: bool = True) -> None:
        self.name = name
        self.account_id = None
        self.enabled = enabled
        self.sent: list[Signal] = []
        self.alerts: list[str] = []
        self.closed = 0

    async def execute(self, signal: Signal) -> OrderResult:
        self.sent.append(signal)
        return _dry_run_result(self.name, self.account_id, signal, "notify")

    async def alert(self, message: str) -> None:
        self.alerts.append(message)
        logger.warning("dry run alert %s: %s", self.name, message)

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed += 1
