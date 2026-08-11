from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Position, SignalIntent
from nq_agent.strategy.always import AlwaysStrategy
from nq_agent.strategy.stub import StubStrategy

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)


def calendar() -> SessionCalendar:
    return SessionCalendar("America/New_York", time(9, 30), time(16, 30))


def context(now: datetime = OPEN, history: int = 500) -> Context:
    return Context(SimClock(now), calendar(), history)


def bar(minute: int, close: str = "20100", timeframe: str = "1m") -> Bar:
    return Bar(
        symbol="NQ",
        timeframe=timeframe,
        open_time=OPEN + timedelta(minutes=minute),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
    )


def test_context_returns_most_recent_bars_oldest_first() -> None:
    ctx = context()
    for minute in range(5):
        ctx.record_bar(bar(minute, close=str(20100 + minute)))

    recent = ctx.bars("1m", 3)
    assert [b.close for b in recent] == [Decimal("20102"), Decimal("20103"), Decimal("20104")]


def test_context_separates_timeframes() -> None:
    ctx = context()
    ctx.record_bar(bar(0, timeframe="1m"))
    ctx.record_bar(bar(0, timeframe="5m"))
    assert len(ctx.bars("1m", 10)) == 1
    assert len(ctx.bars("5m", 10)) == 1


def test_context_history_is_bounded() -> None:
    ctx = context(history=3)
    for minute in range(10):
        ctx.record_bar(bar(minute))
    assert len(ctx.bars("1m", 100)) == 3


def test_context_exposes_session_date_from_the_clock() -> None:
    ctx = context(now=datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc))
    assert ctx.session_date == date(2026, 7, 15)


def test_context_position_and_counters_round_trip() -> None:
    ctx = context()
    assert ctx.position is None
    assert ctx.trades_taken == 0
    assert ctx.is_warmup is False

    position = Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        entry_price=Decimal("20100"),
        entry_time=OPEN,
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
    )
    ctx.set_position(position)
    ctx.set_trades_taken(2)
    ctx.set_warmup(True)

    assert ctx.position == position
    assert ctx.trades_taken == 2
    assert ctx.is_warmup is True


def test_stub_strategy_never_fires() -> None:
    strategy = StubStrategy()
    ctx = context()
    strategy.on_session_start(date(2026, 7, 15))
    for minute in range(20):
        assert strategy.on_bar(bar(minute), ctx) is None


def test_always_strategy_fires_once_per_session() -> None:
    strategy = AlwaysStrategy()
    ctx = context()
    strategy.on_session_start(date(2026, 7, 15))

    first = strategy.on_bar(bar(0, close="20100"), ctx)
    assert first is not None
    assert first.intent is SignalIntent.ENTRY
    assert first.direction is Direction.LONG
    assert first.entry_price == Decimal("20100")
    assert first.stop_price == Decimal("20090")
    assert first.target_price == Decimal("20120")
    assert first.quantity == 1

    assert strategy.on_bar(bar(1, close="20105"), ctx) is None
    assert strategy.on_bar(bar(2, close="20110"), ctx) is None


def test_always_strategy_ignores_non_base_timeframes() -> None:
    strategy = AlwaysStrategy()
    ctx = context()
    strategy.on_session_start(date(2026, 7, 15))
    assert strategy.on_bar(bar(0, timeframe="5m"), ctx) is None


def test_always_strategy_rearms_on_the_next_session() -> None:
    strategy = AlwaysStrategy()
    ctx = context()

    strategy.on_session_start(date(2026, 7, 15))
    assert strategy.on_bar(bar(0), ctx) is not None
    strategy.on_session_end(date(2026, 7, 15))

    strategy.on_session_start(date(2026, 7, 16))
    assert strategy.on_bar(bar(0), ctx) is not None


def test_always_strategy_state_round_trips() -> None:
    strategy = AlwaysStrategy()
    ctx = context()
    strategy.on_session_start(date(2026, 7, 15))
    assert strategy.on_bar(bar(0), ctx) is not None

    saved = strategy.get_state()

    restored = AlwaysStrategy()
    restored.on_session_start(date(2026, 7, 15))
    restored.restore_state(saved)
    assert restored.on_bar(bar(1), ctx) is None
