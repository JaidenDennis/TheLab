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
        It is the caller (Engine.run) that turns resume_from into behaviour,
        by classifying every bar it receives into one of three windows:

          1. close_time <= resume_from: already reflected in the SessionState
             the caller restored before streaming began (strategy_state,
             PositionTracker's position, trades_taken). The engine skips
             these entirely -- no strategy call, no position tracking, no
             persist -- because replaying them would double-count state that
             is already there.
          2. resume_from < close_time <= backfill_until: the real gap between
             the crash and this process starting back up. The engine runs
             the strategy and position tracker for real, so in-memory state
             catches up to the restored snapshot, but suppresses any signal
             either produces -- its moment has already passed, and dispatching
             it now would chase a stale entry or double a live feed's own
             backfill dispatch.
          3. Everything else (or resume_from is None, or backfill_until is
             None): live. Signals reach the router normally.

        A feed must therefore emit every bar from resume_from forward, not
        skip straight past it: window 2 only has bars in it if the feed
        actually produces them. This deterministic implementation (see
        ReplayFeed) replays from the top and never sets a distinct backfill
        boundary, so window 2 is empty for it by construction -- a replay
        restart is a deterministic continuation, not a catch-up. A live feed
        backfilling from resume_from up to the moment streaming starts is
        exactly what populates window 2.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any provider resources."""
