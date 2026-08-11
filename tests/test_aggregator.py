from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nq_agent.feed.aggregator import BarAggregator
from nq_agent.models import TIMEFRAME_SECONDS, Bar, Tick

START = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)


def tick(offset_seconds: int, price: str, size: int = 1) -> Tick:
    return Tick(
        symbol="NQ",
        ts=START + timedelta(seconds=offset_seconds),
        price=Decimal(price),
        size=size,
    )


def test_no_bar_emitted_until_the_bucket_closes() -> None:
    agg = BarAggregator("NQ", ["1m"])
    assert agg.add_tick(tick(0, "20100")) == []
    assert agg.add_tick(tick(30, "20110")) == []
    assert agg.add_tick(tick(59, "20105")) == []


def test_bar_ohlcv_is_correct_on_close() -> None:
    agg = BarAggregator("NQ", ["1m"])
    agg.add_tick(tick(0, "20100", 2))
    agg.add_tick(tick(20, "20120", 3))
    agg.add_tick(tick(40, "20090", 1))
    agg.add_tick(tick(59, "20105", 4))

    bars = agg.add_tick(tick(60, "20106"))
    assert len(bars) == 1
    bar = bars[0]
    assert bar.timeframe == "1m"
    assert bar.open_time == START
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("20100"),
        Decimal("20120"),
        Decimal("20090"),
        Decimal("20105"),
    )
    assert bar.volume == 10
    assert bar.closed is True


def test_one_minute_emitted_before_five_minute_on_shared_boundary() -> None:
    agg = BarAggregator("NQ", ["1m", "5m"])
    for second in range(0, 300, 30):
        agg.add_tick(tick(second, "20100"))

    bars = agg.add_tick(tick(300, "20200"))
    assert [b.timeframe for b in bars] == ["1m", "5m"]
    assert bars[0].close_time == bars[1].close_time


def test_five_minute_aggregates_the_whole_window() -> None:
    agg = BarAggregator("NQ", ["1m", "5m"])
    agg.add_tick(tick(10, "20100", 1))
    agg.add_tick(tick(130, "20150", 1))
    agg.add_tick(tick(290, "20080", 1))

    bars = agg.add_tick(tick(300, "20090"))
    five = next(b for b in bars if b.timeframe == "5m")
    assert five.open == Decimal("20100")
    assert five.high == Decimal("20150")
    assert five.low == Decimal("20080")
    assert five.close == Decimal("20080")
    assert five.volume == 3


def test_quiet_minutes_produce_no_synthetic_bars() -> None:
    agg = BarAggregator("NQ", ["1m"])
    agg.add_tick(tick(0, "20100"))
    bars = agg.add_tick(tick(400, "20200"))
    assert len(bars) == 1
    assert bars[0].open_time == START


def test_flush_closes_the_open_buckets() -> None:
    agg = BarAggregator("NQ", ["1m", "5m"])
    agg.add_tick(tick(10, "20100"))
    bars = agg.flush()
    assert [b.timeframe for b in bars] == ["1m", "5m"]
    assert agg.flush() == []


def test_out_of_order_ticks_are_rejected() -> None:
    agg = BarAggregator("NQ", ["1m"])
    agg.add_tick(tick(60, "20100"))
    with pytest.raises(ValueError, match="out of order"):
        agg.add_tick(tick(30, "20100"))


def test_unknown_timeframe_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unsupported timeframe"):
        BarAggregator("NQ", ["3m"])


# --- Supplementary correctness coverage -------------------------------------
#
# The four tests below were added during self-review, beyond the brief's
# verbatim list, to lock in specific lookahead-adjacent properties the task
# flagged as critical: exact-boundary exclusion, identical timestamps, global
# ordering over a long/gappy multi-timeframe run, and flush()'s tie-break
# discipline. They exercise the existing implementation as written and did
# not require any implementation change.


def test_boundary_tick_opens_the_next_bucket_not_the_previous() -> None:
    agg = BarAggregator("NQ", ["1m"])
    agg.add_tick(tick(0, "20100"))
    closed = agg.add_tick(tick(60, "20106"))

    assert closed[0].close == Decimal("20100")
    assert closed[0].volume == 1

    next_bar = agg.flush()[0]
    assert next_bar.open_time == START + timedelta(seconds=60)
    assert (next_bar.open, next_bar.high, next_bar.low, next_bar.close) == (
        Decimal("20106"),
        Decimal("20106"),
        Decimal("20106"),
        Decimal("20106"),
    )
    assert next_bar.volume == 1


def test_identical_timestamps_merge_into_one_bar() -> None:
    agg = BarAggregator("NQ", ["1m"])
    first = tick(10, "20100")
    agg.add_tick(first)
    twin = Tick(symbol="NQ", ts=first.ts, price=Decimal("20200"), size=5)
    assert agg.add_tick(twin) == []

    bar = agg.flush()[0]
    assert bar.open == Decimal("20100")
    assert bar.high == Decimal("20200")
    assert bar.low == Decimal("20100")
    assert bar.close == Decimal("20200")
    assert bar.volume == 6


def test_flush_orders_same_close_time_bars_by_duration() -> None:
    agg = BarAggregator("NQ", ["1m", "5m"])
    # A tick in the last minute of the first 5m window leaves both a 1m and
    # a 5m bucket open with the same eventual close_time (START + 300s).
    agg.add_tick(tick(250, "20100"))

    bars = agg.flush()
    assert [b.timeframe for b in bars] == ["1m", "5m"]
    assert bars[0].close_time == bars[1].close_time == START + timedelta(seconds=300)


def test_long_run_never_violates_close_time_ordering() -> None:
    agg = BarAggregator("NQ", ["1m", "5m", "15m"])
    # Irregular arrivals over 20 minutes with two multi-minute quiet gaps
    # (no ticks in [140, 260) or [610, 730)), crossing several 1m, 5m and
    # one 15m boundary.
    offsets = [o for o in range(0, 1200, 17) if not (140 <= o < 260) and not (610 <= o < 730)]

    all_bars: list[Bar] = []
    price = 20000
    for offset in offsets:
        price += 1
        all_bars.extend(agg.add_tick(tick(offset, str(price))))
    all_bars.extend(agg.flush())

    keys = [(bar.close_time, TIMEFRAME_SECONDS[bar.timeframe]) for bar in all_bars]
    assert keys == sorted(keys)
    assert {bar.timeframe for bar in all_bars} == {"1m", "5m", "15m"}
    assert len(all_bars) > 10
