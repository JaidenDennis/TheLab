from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.context import Context
from nq_agent.models import Bar, Direction
from nq_agent.strategy.orb import OpeningRangeBreakout

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)


def context() -> Context:
    return Context(
        SimClock(OPEN),
        SessionCalendar("America/New_York", datetime(2026, 1, 1, 9, 30).time(),
                        datetime(2026, 1, 1, 16, 30).time()),
        500,
    )


def bar(minute: int, high: str, low: str, close: str) -> Bar:
    return Bar(
        symbol="NQ",
        timeframe="1m",
        open_time=OPEN + timedelta(minutes=minute),
        open=Decimal(low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10,
    )


def feed(strategy: OpeningRangeBreakout, bars: list[Bar]) -> list[object]:
    ctx = context()
    out = []
    for b in bars:
        ctx.record_bar(b)
        signal = strategy.on_bar(b, ctx)
        if signal is not None:
            out.append(signal)
    return out


def range_bars(minutes: int = 3, high: str = "20110", low: str = "20090") -> list[Bar]:
    """Bars that build a range of exactly [low, high] and stay inside it."""
    bars = [bar(0, high=high, low=low, close="20100")]
    bars += [bar(m, high="20105", low="20095", close="20100") for m in range(1, minutes)]
    return bars


def test_no_signal_while_the_range_is_still_building() -> None:
    strategy = OpeningRangeBreakout(range_minutes=3)

    assert feed(strategy, range_bars(3)) == []


def test_a_close_above_the_range_goes_long() -> None:
    strategy = OpeningRangeBreakout(range_minutes=3)
    bars = range_bars(3) + [bar(3, high="20120", low="20100", close="20115")]

    signals = feed(strategy, bars)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.direction is Direction.LONG
    assert signal.entry_price == Decimal("20115")
    assert signal.stop_price == Decimal("20090")


def test_a_close_below_the_range_goes_short() -> None:
    strategy = OpeningRangeBreakout(range_minutes=3)
    bars = range_bars(3) + [bar(3, high="20095", low="20080", close="20085")]

    signals = feed(strategy, bars)

    assert len(signals) == 1
    assert signals[0].direction is Direction.SHORT
    assert signals[0].stop_price == Decimal("20110")


def test_the_target_is_the_reward_multiple_of_the_risk() -> None:
    strategy = OpeningRangeBreakout(range_minutes=3, reward_multiple=Decimal("2"))
    bars = range_bars(3) + [bar(3, high="20120", low="20100", close="20115")]

    signal = feed(strategy, bars)[0]

    # entry 20115, stop 20090 => risk 25 => target 20115 + 50
    assert signal.target_price == Decimal("20165")


def test_a_close_back_inside_the_range_does_not_fire() -> None:
    strategy = OpeningRangeBreakout(range_minutes=3)
    bars = range_bars(3) + [bar(3, high="20120", low="20095", close="20100")]

    assert feed(strategy, bars) == []


def test_only_one_entry_per_session() -> None:
    strategy = OpeningRangeBreakout(range_minutes=3)
    bars = range_bars(3) + [
        bar(3, high="20120", low="20100", close="20115"),
        bar(4, high="20130", low="20110", close="20125"),
    ]

    assert len(feed(strategy, bars)) == 1


def test_a_new_session_re_arms_the_strategy() -> None:
    strategy = OpeningRangeBreakout(range_minutes=3)
    feed(strategy, range_bars(3) + [bar(3, high="20120", low="20100", close="20115")])

    strategy.on_session_start(date(2026, 7, 16))

    assert strategy.get_state()["fired"] is False
    assert strategy.get_state()["high"] is None


def test_state_survives_a_json_round_trip_as_strings() -> None:
    """The engine persists strategy_state through JSON, so Decimal and
    datetime come back as str. A strategy that assumes otherwise raises on the
    first bar after a crash."""
    import json

    strategy = OpeningRangeBreakout(range_minutes=3)
    feed(strategy, range_bars(3))
    revived = OpeningRangeBreakout(range_minutes=3)

    revived.restore_state(json.loads(json.dumps(strategy.get_state())))

    assert revived.get_state() == strategy.get_state()


def test_a_restored_strategy_fires_exactly_where_the_original_would() -> None:
    """The control-vs-resume check. An accumulating strategy that replays bars
    it has already seen looks fine until it double-counts; this pins that the
    restored range is the same range."""
    import json

    control = OpeningRangeBreakout(range_minutes=3)
    breakout = bar(3, high="20120", low="20100", close="20115")
    feed(control, range_bars(3))
    expected = control.on_bar(breakout, context())

    crashed = OpeningRangeBreakout(range_minutes=3)
    feed(crashed, range_bars(3))
    revived = OpeningRangeBreakout(range_minutes=3)
    revived.restore_state(json.loads(json.dumps(crashed.get_state())))

    actual = revived.on_bar(breakout, context())

    assert actual is not None and expected is not None
    assert (actual.direction, actual.entry_price, actual.stop_price, actual.target_price) == (
        expected.direction,
        expected.entry_price,
        expected.stop_price,
        expected.target_price,
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"range_minutes": 0}, {"reward_multiple": Decimal("0")}, {"quantity": 0}],
)
def test_nonsense_parameters_are_rejected_at_construction(kwargs: dict) -> None:
    """At construction, not on the first bar -- a misconfigured strategy must
    not fail halfway through a live session."""
    with pytest.raises(ValueError):
        OpeningRangeBreakout(**kwargs)
