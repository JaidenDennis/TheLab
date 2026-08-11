from __future__ import annotations

import logging

from nq_agent.execution.base import Executor, NotifyExecutor
from nq_agent.models import OrderResult, Signal

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


class DryRunExecutor(Executor):
    """Logs instead of trading. Produces the same OrderResult shape as the real thing."""

    def __init__(self, name: str, account_id: str | None = None, enabled: bool = True) -> None:
        self.name = f"{name}:{account_id}" if account_id else name
        self.account_id = account_id
        self.enabled = enabled
        self.sent: list[Signal] = []

    async def execute(self, signal: Signal) -> OrderResult:
        self.sent.append(signal)
        payload = _describe(signal)
        logger.info("dry run execute %s %s", self.name, payload)
        return OrderResult(
            signal_id=signal.id,
            executor_name=self.name,
            success=True,
            account_id=self.account_id,
            latency_ms=0,
            raw_response=payload,
        )

    async def health_check(self) -> bool:
        return True


class DryRunNotifier(NotifyExecutor):
    """Stands in for ntfy or Pushover until one is chosen."""

    def __init__(self, name: str, enabled: bool = True) -> None:
        self.name = name
        self.account_id = None
        self.enabled = enabled
        self.sent: list[Signal] = []
        self.alerts: list[str] = []

    async def execute(self, signal: Signal) -> OrderResult:
        self.sent.append(signal)
        payload = _describe(signal)
        logger.info("dry run notify %s %s", self.name, payload)
        return OrderResult(
            signal_id=signal.id,
            executor_name=self.name,
            success=True,
            account_id=None,
            latency_ms=0,
            raw_response=payload,
        )

    async def alert(self, message: str) -> None:
        self.alerts.append(message)
        logger.warning("dry run alert %s: %s", self.name, message)

    async def health_check(self) -> bool:
        return True
