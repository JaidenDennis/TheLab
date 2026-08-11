from datetime import datetime, timezone
from pathlib import Path

from nq_agent.clock import SimClock
from nq_agent.feed.replay import ReplayFeed

FIXTURE = Path("tests/fixtures/2026-07-15.jsonl")
OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)


async def test_stream_yields_only_closed_bars_in_close_time_order() -> None:
    feed = ReplayFeed(FIXTURE, "NQ")
    bars = [bar async for bar in feed.stream("NQ", ["1m", "5m"])]
    await feed.close()

    assert bars, "fixture produced no bars"
    assert all(bar.closed for bar in bars)
    close_times = [bar.close_time for bar in bars]
    assert close_times == sorted(close_times)


async def test_stream_emits_one_minute_before_five_minute_on_shared_boundary() -> None:
    feed = ReplayFeed(FIXTURE, "NQ")
    bars = [bar async for bar in feed.stream("NQ", ["1m", "5m"])]
    await feed.close()

    first_five = next(i for i, bar in enumerate(bars) if bar.timeframe == "5m")
    assert bars[first_five - 1].timeframe == "1m"
    assert bars[first_five - 1].close_time == bars[first_five].close_time


async def test_stream_advances_the_sim_clock_to_each_bar_close() -> None:
    clock = SimClock(OPEN)
    feed = ReplayFeed(FIXTURE, "NQ", clock=clock)

    seen: list[datetime] = []
    async for bar in feed.stream("NQ", ["1m"]):
        assert clock.now() == bar.close_time
        seen.append(clock.now())
    await feed.close()

    assert seen == sorted(seen)
    assert clock.now() == CLOSE


async def test_get_bars_returns_the_requested_window_only() -> None:
    feed = ReplayFeed(FIXTURE, "NQ")
    start = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 15, 14, 10, tzinfo=timezone.utc)

    bars = await feed.get_bars("NQ", "1m", start, end)
    await feed.close()

    assert len(bars) == 10
    assert bars[0].open_time == start
    assert bars[-1].open_time == datetime(2026, 7, 15, 14, 9, tzinfo=timezone.utc)
    assert all(bar.timeframe == "1m" for bar in bars)


async def test_the_sessions_final_bar_reaches_the_true_close() -> None:
    """Was test_final_partial_bucket_is_flushed: the fixture used to end five
    seconds short of ever closing its last 1m bucket, so this only passed
    because flush() fabricated a closed bar out of an incomplete one. The
    fixture now carries one real closing tick at 20:30:00 UTC (see
    make_fixture.py), so the last bar closes via ordinary rollover, same as
    every other bar -- nothing here is flushed anymore. Renamed accordingly;
    see test_a_bucket_left_open_by_the_last_tick_is_not_yielded below for
    the actual partial-bucket-dropping behaviour this pins.
    """
    feed = ReplayFeed(FIXTURE, "NQ")
    bars = [bar async for bar in feed.stream("NQ", ["1m"])]
    await feed.close()
    assert bars[-1].close_time == CLOSE
    assert bars[-1].closed is True


async def test_a_bucket_left_open_by_the_last_tick_is_not_yielded(tmp_path: Path) -> None:
    """Required addition (Minor 1, second half): DataFeed promises closed
    bars only. A tick stream ending mid-bucket must not produce a bar for
    that bucket at all -- confirmed here with a fixture built to end
    mid-minute, deliberately, unlike the real fixture above.
    """
    ticks = tmp_path / "ticks.jsonl"
    ticks.write_text(
        "\n".join(
            [
                '{"ts": "2026-07-15T13:30:00+00:00", "price": "100.00", "size": 1}',
                '{"ts": "2026-07-15T13:30:30+00:00", "price": "101.00", "size": 1}',
            ]
        )
        + "\n"
    )
    feed = ReplayFeed(ticks, "NQ")

    bars = [bar async for bar in feed.stream("NQ", ["1m"])]
    await feed.close()

    assert bars == [], "the only bucket here never saw a tick at or after its close_time"


async def test_resume_from_is_accepted_and_ignored_by_replay() -> None:
    feed = ReplayFeed(FIXTURE, "NQ")
    resumed = [
        bar
        async for bar in feed.stream(
            "NQ", ["1m"], datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
        )
    ]
    await feed.close()
    assert resumed[0].open_time == OPEN


def test_first_tick_time_reads_only_the_first_line() -> None:
    assert ReplayFeed(FIXTURE, "NQ").first_tick_time() == OPEN
