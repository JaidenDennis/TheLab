from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from nq_agent.models import Bar


class DataFeed(ABC):
    """Source of closed bars.

    Implementations never emit a partial bar. When the provider streams ticks,
    the aggregator holds each bucket until it closes. Every lookahead bug
    starts with a partial bar reaching the strategy.
    """

    @abstractmethod
    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        """Historical closed bars with open_time in [start, end)."""

    @abstractmethod
    def stream(
        self, symbol: str, timeframes: list[str], resume_from: datetime | None = None
    ) -> AsyncIterator[Bar]:
        """Closed bars across all requested timeframes, ordered by close time.

        When two timeframes close on the same boundary the shorter one is
        yielded first. A 5m arriving before its own final 1m is a lookahead bug.

        When resume_from is set the feed must emit history from that point
        before live data, so crash recovery and a cold start share one path.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any provider resources."""
