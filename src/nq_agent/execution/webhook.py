"""HTTP webhook executor.

VERIFICATION STATUS. The HTTP mechanics, timeout handling, outcome
classification and session lifecycle here are covered by tests against a real
local aiohttp server. What is NOT verified is the payload contract: the exact
field names and order semantics your broker's webhook expects are vendor
specific, and no request has ever been sent to one. `_build_payload` is
therefore isolated and marked -- it is the one method to check against the
vendor's docs before this touches an account.

The outcome classification is the load-bearing part:

  2xx                     -> FILLED
  4xx (except 408/429)    -> REJECTED. The broker understood and said no.
  408, 429, 5xx           -> UNKNOWN. The request may have been actioned.
  timeout / transport     -> UNKNOWN. Nothing about the outcome is known.

A 500 is UNKNOWN rather than REJECTED on purpose: the order may well have been
accepted before whatever failed downstream. Calling that a rejection tells the
operator a position does not exist when it might.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nq_agent.execution.base import Executor
from nq_agent.models import Direction, OrderOutcome, OrderResult, Signal, SignalIntent

if TYPE_CHECKING:
    import aiohttp

logger = logging.getLogger(__name__)

# 408 Request Timeout and 429 Too Many Requests are 4xx but say nothing about
# whether the order was actioned, so they are not rejections.
AMBIGUOUS_4XX = frozenset({408, 429})


def classify(status: int) -> OrderOutcome:
    if 200 <= status < 300:
        return OrderOutcome.FILLED
    if 400 <= status < 500 and status not in AMBIGUOUS_4XX:
        return OrderOutcome.REJECTED
    return OrderOutcome.UNKNOWN


class WebhookExecutor(Executor):
    def __init__(
        self,
        name: str,
        url: str,
        token: str | None = None,
        account_id: str | None = None,
        enabled: bool = True,
        timeout: float = 5.0,
    ) -> None:
        if not url:
            raise ValueError(f"executor {name!r} needs a webhook URL")
        self.name = f"{name}:{account_id}" if account_id is not None else name
        self.account_id = account_id
        self.enabled = enabled
        self._url = url
        self._token = token
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """One session for the executor's lifetime.

        A session per request would rebuild the TLS connection on every order,
        putting a handshake between the signal and the fill. This is exactly
        the resource Executor.close() exists for.
        """
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        return self._session

    def _build_payload(self, signal: Signal) -> dict[str, Any]:
        """VENDOR SPECIFIC -- check this against the broker's docs first.

        Prices go out as strings, not floats: a price that has survived
        Decimal all the way from the strategy must not be turned into a
        float in the last three lines before it leaves the process.

        A FLATTEN inverts the direction, per the Signal contract: `direction`
        on a FLATTEN is the direction of the position being closed, so
        closing a LONG sends a sell.
        """
        closing = signal.intent is SignalIntent.FLATTEN
        side = signal.direction
        if closing:
            side = Direction.SHORT if side is Direction.LONG else Direction.LONG

        payload: dict[str, Any] = {
            "signal_id": signal.id,
            "symbol": signal.symbol,
            "action": "close" if closing else "open",
            "side": "buy" if side is Direction.LONG else "sell",
            "quantity": signal.quantity,
            "timestamp": signal.timestamp.isoformat(),
        }
        if self.account_id is not None:
            payload["account"] = self.account_id
        if not closing:
            assert signal.entry_price is not None
            assert signal.stop_price is not None
            assert signal.target_price is not None
            payload["entry"] = str(signal.entry_price)
            payload["stop"] = str(signal.stop_price)
            payload["target"] = str(signal.target_price)
        return payload

    async def execute(self, signal: Signal) -> OrderResult:
        """Send the order. Never raises -- returns a classified result."""
        import aiohttp

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            session = await self._get_session()
            async with session.post(
                self._url, json=self._build_payload(signal), headers=headers
            ) as response:
                body = await response.text()
                outcome = classify(response.status)
                return OrderResult(
                    signal_id=signal.id,
                    executor_name=self.name,
                    outcome=outcome,
                    account_id=self.account_id,
                    error=None if outcome is OrderOutcome.FILLED else f"HTTP {response.status}",
                    raw_response={"status": response.status, "body": body[:2000]},
                )
        except asyncio.TimeoutError:
            # UNKNOWN, not REJECTED: the order may be sitting in the broker's
            # queue right now.
            return OrderResult(
                signal_id=signal.id,
                executor_name=self.name,
                outcome=OrderOutcome.UNKNOWN,
                account_id=self.account_id,
                error="timeout",
            )
        except aiohttp.ClientError as exc:
            # Transport failure. Whether the request reached the broker before
            # the connection died is exactly what cannot be determined here.
            return OrderResult(
                signal_id=signal.id,
                executor_name=self.name,
                outcome=OrderOutcome.UNKNOWN,
                account_id=self.account_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(self._url) as response:
                return response.status < 500
        except Exception:  # noqa: BLE001 - a health check never breaks startup
            logger.warning("health check failed for %s", self.name)
            return False

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
