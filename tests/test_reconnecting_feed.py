import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nq_agent.feed.base import DataFeed
from nq_agent.feed.reconnecting import ReconnectingFeed
from nq_agent.models import Bar

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)


def bar(minute: int) -> Bar:
    return Bar(
        symbol="NQ",
        timeframe="1m",
        open_time=OPEN + timedelta(minutes=minute),
        open=Decimal("20100"),
        high=Decimal("20105"),
        low=Decimal("20095"),
        close=Decimal("20100"),
        volume=10,
    )


class FlakyFeed(DataFeed):
    """Drops after `drop_after` bars, `drops` times, then runs clean."""

    def __init__(self, total: int, drop_after: int, drops: int) -> None:
        self._total = total
        self._drop_after = drop_after
        self._drops_left = drops
        self.resume_points: list[datetime | None] = []
        self.closed = 0

    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        return []

    async def stream(
        self, symbol: str, timeframes: list[str], resume_from: datetime | None = None
    ) -> AsyncIterator[Bar]:
        self.resume_points.append(resume_from)
        start = 0
        if resume_from is not None:
            # Emit only bars after the resume point, the way a real feed would.
            start = int((resume_from - OPEN).total_seconds() // 60)
        for emitted, minute in enumerate(range(start, self._total)):
            if self._drops_left > 0 and emitted >= self._drop_after:
                self._drops_left -= 1
                raise ConnectionError("feed dropped")
            yield bar(minute)

    async def close(self) -> None:
        self.closed += 1


async def drain(feed: DataFeed) -> list[Bar]:
    return [b async for b in feed.stream("NQ", ["1m"])]


async def test_a_clean_feed_passes_straight_through() -> None:
    feed = ReconnectingFeed(FlakyFeed(total=5, drop_after=99, drops=0), initial_backoff=0.01)

    bars = await drain(feed)

    assert len(bars) == 5
    assert feed.attempts == 0


async def test_a_dropped_feed_reconnects_and_finishes() -> None:
    feed = ReconnectingFeed(FlakyFeed(total=5, drop_after=2, drops=1), initial_backoff=0.01)

    bars = await drain(feed)

    assert [b.open_time.minute for b in bars] == [30, 31, 32, 33, 34]
    assert feed.attempts == 1


async def test_no_bar_is_delivered_twice_across_a_reconnect() -> None:
    """The failure that matters. Reconnecting from the caller's original
    resume_from replays the whole session through the strategy again -- the
    same double-counting the resume windows exist to prevent."""
    feed = ReconnectingFeed(FlakyFeed(total=6, drop_after=3, drops=1), initial_backoff=0.01)

    bars = await drain(feed)

    minutes = [b.open_time for b in bars]
    assert len(minutes) == len(set(minutes)), f"duplicate bars delivered: {minutes}"


async def test_the_reconnect_resumes_from_the_last_bar_delivered() -> None:
    inner = FlakyFeed(total=6, drop_after=3, drops=1)
    await drain(ReconnectingFeed(inner, initial_backoff=0.01))

    assert inner.resume_points[0] is None
    assert inner.resume_points[1] == OPEN + timedelta(minutes=3), (
        "the second connect must ask for bars after the last one delivered"
    )


async def test_backoff_grows_between_attempts() -> None:
    feed = ReconnectingFeed(
        FlakyFeed(total=99, drop_after=0, drops=3),
        initial_backoff=1.0,
        backoff_factor=2.0,
    )

    assert [feed._backoff(n) for n in range(4)] == [1.0, 2.0, 4.0, 8.0]


async def test_backoff_is_capped() -> None:
    feed = ReconnectingFeed(
        FlakyFeed(total=1, drop_after=0, drops=0),
        initial_backoff=1.0,
        backoff_factor=10.0,
        max_backoff=5.0,
    )

    assert feed._backoff(10) == 5.0


async def test_giving_up_raises_rather_than_ending_the_stream_quietly() -> None:
    """A feed that has stopped working must not look like a feed that reached
    the end of the day. Silently returning would let the engine run its
    teardown and report a clean session."""
    feed = ReconnectingFeed(
        FlakyFeed(total=99, drop_after=0, drops=99),
        max_attempts=3,
        initial_backoff=0.01,
    )

    with pytest.raises(ConnectionError):
        await drain(feed)

    assert feed.attempts == 3


async def test_each_drop_is_reported() -> None:
    seen: list[tuple[int, str]] = []

    async def on_error(attempt: int, exc: Exception) -> None:
        seen.append((attempt, type(exc).__name__))

    feed = ReconnectingFeed(
        FlakyFeed(total=6, drop_after=2, drops=2),
        initial_backoff=0.01,
        on_error=on_error,
    )

    await drain(feed)

    # Both drops report attempt 1, not 1 then 2: the number is the count of
    # CONSECUTIVE failures, and bars arrived in between, so each drop is a
    # first failure. A running total would make the backoff escalate on a
    # feed that is recovering fine.
    assert seen == [(1, "ConnectionError"), (1, "ConnectionError")]
    assert feed.attempts == 2, "the lifetime counter does keep climbing"


async def test_the_attempt_counter_resets_after_a_bar_arrives() -> None:
    """Reset on a successful bar, not on a successful connect. A provider that
    accepts the connection and immediately drops would otherwise spin forever
    with the backoff permanently reset to zero."""
    feed = ReconnectingFeed(
        FlakyFeed(total=9, drop_after=2, drops=3),
        max_attempts=2,
        initial_backoff=0.01,
    )

    bars = await drain(feed)

    assert len(bars) == 9


async def test_cancellation_is_not_treated_as_a_disconnect() -> None:
    """Shutdown must not be retried. Swallowing CancelledError here keeps the
    process alive through a SIGTERM."""

    class HangingFeed(DataFeed):
        async def get_bars(
            self, symbol: str, timeframe: str, start: datetime, end: datetime
        ) -> list[Bar]:
            return []

        async def stream(
            self, symbol: str, timeframes: list[str], resume_from: datetime | None = None
        ) -> AsyncIterator[Bar]:
            yield bar(0)
            await asyncio.sleep(60)

        async def close(self) -> None:
            return None

    feed = ReconnectingFeed(HangingFeed(), initial_backoff=0.01)
    task = asyncio.create_task(drain(feed))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert feed.attempts == 0


async def test_close_reaches_the_wrapped_feed() -> None:
    inner = FlakyFeed(total=1, drop_after=99, drops=0)
    feed = ReconnectingFeed(inner, initial_backoff=0.01)

    await feed.close()

    assert inner.closed == 1


def test_nonsense_configuration_is_rejected() -> None:
    inner = FlakyFeed(total=1, drop_after=99, drops=0)

    with pytest.raises(ValueError):
        ReconnectingFeed(inner, max_attempts=0)
    with pytest.raises(ValueError):
        ReconnectingFeed(inner, initial_backoff=0)
