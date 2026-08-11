from datetime import datetime, timedelta, timezone
from decimal import Decimal

from nq_agent.models import Bar, Direction, Position, Signal, SignalIntent
from nq_agent.position import PositionTracker

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)


def bar(minute: int, high: str, low: str, close: str) -> Bar:
    return Bar(
        symbol="NQ",
        timeframe="1m",
        open_time=OPEN + timedelta(minutes=minute),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10,
    )


def long_entry() -> Signal:
    return Signal(
        timestamp=OPEN + timedelta(minutes=1),
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.LONG,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
        quantity=1,
        reason="test",
    )


def short_entry() -> Signal:
    return Signal(
        timestamp=OPEN + timedelta(minutes=1),
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.SHORT,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20110"),
        target_price=Decimal("20080"),
        quantity=1,
        reason="test",
    )


def test_starts_flat() -> None:
    assert PositionTracker().position is None


def test_entry_signal_opens_a_position_at_the_signal_price() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    position = tracker.position
    assert position is not None
    assert position.direction is Direction.LONG
    assert position.entry_price == Decimal("20100")
    assert position.entry_time == OPEN + timedelta(minutes=1)


def test_untouched_bar_leaves_the_position_open() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())
    assert tracker.on_bar(bar(2, high="20115", low="20095", close="20110")) is None
    assert tracker.position is not None


def test_long_target_touch_closes_at_the_target() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    closed = tracker.on_bar(bar(2, high="20125", low="20098", close="20122"))
    assert closed is not None
    assert closed.exit_reason == "TARGET"
    assert closed.exit_price == Decimal("20120")
    assert tracker.position is None


def test_long_stop_touch_closes_at_the_stop() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    closed = tracker.on_bar(bar(2, high="20105", low="20085", close="20088"))
    assert closed is not None
    assert closed.exit_reason == "STOP"
    assert closed.exit_price == Decimal("20090")


def test_stop_wins_when_one_bar_touches_both() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    closed = tracker.on_bar(bar(2, high="20130", low="20080", close="20125"))
    assert closed is not None
    assert closed.exit_reason == "STOP"


def test_short_stop_and_target_are_mirrored() -> None:
    tracker = PositionTracker()
    tracker.on_signal(short_entry())
    closed = tracker.on_bar(bar(2, high="20105", low="20075", close="20078"))
    assert closed is not None
    assert closed.exit_reason == "TARGET"
    assert closed.exit_price == Decimal("20080")

    tracker = PositionTracker()
    tracker.on_signal(short_entry())
    closed = tracker.on_bar(bar(2, high="20115", low="20095", close="20112"))
    assert closed is not None
    assert closed.exit_reason == "STOP"
    assert closed.exit_price == Decimal("20110")


def test_flatten_closes_at_the_supplied_price() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    closed = tracker.flatten(Decimal("20107"), OPEN + timedelta(minutes=400))
    assert closed is not None
    assert closed.exit_reason == "FLATTEN"
    assert closed.exit_price == Decimal("20107")
    assert tracker.position is None


def test_flatten_while_flat_is_a_no_op() -> None:
    assert PositionTracker().flatten(Decimal("20100"), OPEN) is None


def test_bars_are_ignored_while_flat() -> None:
    assert PositionTracker().on_bar(bar(2, high="99999", low="1", close="20100")) is None


def test_a_second_entry_while_open_is_ignored() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())
    first = tracker.position
    tracker.on_signal(short_entry())
    assert tracker.position == first


def test_restore_reinstates_a_position() -> None:
    tracker = PositionTracker()
    position = Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        entry_price=Decimal("20100"),
        entry_time=OPEN,
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
    )
    tracker.restore(position)
    assert tracker.position == position

    closed = tracker.on_bar(bar(2, high="20105", low="20085", close="20088"))
    assert closed is not None
    assert closed.exit_reason == "STOP"


def test_exact_touch_on_stop_or_target_counts_as_a_hit() -> None:
    # Each bar below touches exactly one boundary and stays strictly clear of
    # the other, isolating <= / >= from the separate stop-wins tie-break.
    long_stop = PositionTracker()
    long_stop.on_signal(long_entry())
    closed = long_stop.on_bar(bar(2, high="20110", low="20090", close="20095"))
    assert closed is not None
    assert closed.exit_reason == "STOP"
    assert closed.exit_price == Decimal("20090")

    long_target = PositionTracker()
    long_target.on_signal(long_entry())
    closed = long_target.on_bar(bar(2, high="20120", low="20095", close="20115"))
    assert closed is not None
    assert closed.exit_reason == "TARGET"
    assert closed.exit_price == Decimal("20120")

    short_stop = PositionTracker()
    short_stop.on_signal(short_entry())
    closed = short_stop.on_bar(bar(2, high="20110", low="20090", close="20105"))
    assert closed is not None
    assert closed.exit_reason == "STOP"
    assert closed.exit_price == Decimal("20110")

    short_target = PositionTracker()
    short_target.on_signal(short_entry())
    closed = short_target.on_bar(bar(2, high="20105", low="20080", close="20085"))
    assert closed is not None
    assert closed.exit_reason == "TARGET"
    assert closed.exit_price == Decimal("20080")


def test_on_bar_ignores_a_bar_for_a_different_symbol() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    other_symbol_bar = Bar(
        symbol="ES",
        timeframe="1m",
        open_time=OPEN + timedelta(minutes=2),
        open=Decimal("20080"),
        high=Decimal("20080"),
        low=Decimal("20080"),
        close=Decimal("20080"),
        volume=10,
    )
    assert tracker.on_bar(other_symbol_bar) is None
    assert tracker.position is not None
    assert tracker.position.symbol == "NQ"


def test_restore_then_exit_preserves_the_restored_entry_details() -> None:
    tracker = PositionTracker()
    position = Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=3,
        entry_price=Decimal("20055"),
        entry_time=OPEN - timedelta(days=1),
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
    )
    tracker.restore(position)

    closed = tracker.on_bar(bar(2, high="20105", low="20085", close="20088"))
    assert closed is not None
    assert closed.exit_reason == "STOP"
    assert closed.position == position
