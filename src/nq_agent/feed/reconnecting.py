"""A DataFeed that survives its provider dropping the connection.

The design names exponential-backoff reconnect as the response to a feed
disconnect, and nothing implemented it -- ReplayFeed cannot disconnect, so
there was no way to even write the test. This wraps any DataFeed and is
deliberately provider-agnostic: reconnect logic is the part that has to be
right, and it should not have to be re-verified for every provider.

The subtlety is `resume_from`. On reconnect the wrapper does not restart the
stream from where the caller originally asked; it restarts from the close time
of the last bar it actually delivered. Restarting from the original point
would replay the whole session through the strategy a second time -- the exact
double-counting failure the resume windows exist to prevent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime

from nq_agent.feed.base import DataFeed
from nq_agent.models import Bar

logger = logging.getLogger(__name__)


class ReconnectingFeed(DataFeed):
    def __init__(
        self,
        inner: DataFeed,
        max_attempts: int = 5,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        backoff_factor: float = 2.0,
        on_error: object = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
        if initial_backoff <= 0:
            raise ValueError(f"initial_backoff must be positive, got {initial_backoff}")
        self._inner = inner
        self._max_attempts = max_attempts
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._factor = backoff_factor
        # Called with (attempt, exception) on each drop. The engine passes a
        # journal write so feed_error lands in the record; kept as a plain
        # callable so this module does not depend on the journal.
        self._on_error = on_error
        self.attempts = 0

    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        return await self._inner.get_bars(symbol, timeframe, start, end)

    def _backoff(self, attempt: int) -> float:
        return min(self._initial_backoff * (self._factor**attempt), self._max_backoff)

    async def stream(
        self, symbol: str, timeframes: list[str], resume_from: datetime | None = None
    ) -> AsyncIterator[Bar]:
        attempt = 0
        # Where to restart from: the last bar actually handed to the caller,
        # not the caller's original resume_from. Reconnecting to the original
        # point would re-deliver every bar of the session so far.
        position = resume_from

        while True:
            try:
                async for bar in self._inner.stream(symbol, timeframes, position):
                    position = bar.close_time
                    # Reset only after a bar genuinely arrives. Counting a
                    # successful connect as recovery turns a provider that
                    # accepts then immediately drops into an infinite fast
                    # loop with no backoff at all.
                    attempt = 0
                    yield bar
                return
            except asyncio.CancelledError:
                # Shutdown, not a feed failure. Retrying here would ignore the
                # cancellation and keep the process alive.
                raise
            except Exception as exc:  # noqa: BLE001 - any provider error is a disconnect
                attempt += 1
                self.attempts += 1
                if self._on_error is not None:
                    await self._notify(attempt, exc)
                if attempt >= self._max_attempts:
                    logger.error(
                        "feed failed %d times, giving up: %r", attempt, exc
                    )
                    raise
                delay = self._backoff(attempt - 1)
                logger.warning(
                    "feed dropped (%r), reconnecting in %.1fs from %s "
                    "[attempt %d/%d]",
                    exc,
                    delay,
                    position,
                    attempt,
                    self._max_attempts,
                )
                await asyncio.sleep(delay)

    async def _notify(self, attempt: int, exc: Exception) -> None:
        callback = self._on_error
        assert callback is not None
        result = callback(attempt, exc)  # type: ignore[operator]
        if asyncio.iscoroutine(result):
            await result

    async def close(self) -> None:
        await self._inner.close()
