from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.models import Direction, Position, SessionState
from nq_agent.state import StateStore


def position() -> Position:
    return Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        entry_price=Decimal("20100.25"),
        entry_time=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        stop_price=Decimal("20090.25"),
        target_price=Decimal("20120.25"),
    )


async def test_load_returns_none_when_nothing_saved(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()
    assert await store.load(date(2026, 7, 15)) is None


async def test_save_then_load_round_trips_every_field(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()

    state = SessionState(
        session_date=date(2026, 7, 15),
        trades_taken=2,
        is_halted=True,
        strategy_state={"fired": True, "count": 3},
        last_bar_time=datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc),
        position=position(),
    )
    await store.save(state)

    loaded = await store.load(date(2026, 7, 15))
    assert loaded == state
    assert loaded is not None
    assert loaded.position is not None
    assert loaded.position.entry_price == Decimal("20100.25")


async def test_save_overwrites_the_same_session_date(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()

    await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=1))
    await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=2))

    loaded = await store.load(date(2026, 7, 15))
    assert loaded is not None
    assert loaded.trades_taken == 2


async def test_sessions_are_isolated_by_date(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()

    await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=1))
    await store.save(SessionState(session_date=date(2026, 7, 16), trades_taken=9))

    first = await store.load(date(2026, 7, 15))
    second = await store.load(date(2026, 7, 16))
    assert first is not None and first.trades_taken == 1
    assert second is not None and second.trades_taken == 9


async def test_init_schema_creates_the_parent_directory(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "nested" / "state.db")
    await store.init_schema()
    assert (tmp_path / "nested" / "state.db").exists()


async def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()
    await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=4))
    await store.init_schema()

    loaded = await store.load(date(2026, 7, 15))
    assert loaded is not None
    assert loaded.trades_taken == 4
