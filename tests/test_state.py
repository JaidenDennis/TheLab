import asyncio
import sqlite3
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

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


async def test_decimal_survives_the_round_trip_with_full_precision(tmp_path: Path) -> None:
    """20100.1... is not exactly representable in binary floating point, so a
    bug that routed the price through a float anywhere in the pipeline would
    silently change it. Equality with the original Decimal — not merely a
    numerically close value — is the point of this test.
    """
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()

    tricky_price = Decimal("20100.10000000000000000001")
    assert Decimal(str(float(tricky_price))) != tricky_price  # float would truncate this

    state = SessionState(
        session_date=date(2026, 7, 15),
        position=Position(
            symbol="NQ",
            direction=Direction.LONG,
            quantity=1,
            entry_price=tricky_price,
            entry_time=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
            stop_price=Decimal("20090.25"),
            target_price=Decimal("20120.25"),
        ),
    )
    await store.save(state)

    loaded = await store.load(date(2026, 7, 15))
    assert loaded is not None
    assert loaded.position is not None
    assert loaded.position.entry_price == tricky_price
    assert str(loaded.position.entry_price) == str(tricky_price)
    assert not isinstance(loaded.position.entry_price, float)


async def test_optional_fields_round_trip_as_none_not_a_sentinel(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()

    state = SessionState(session_date=date(2026, 7, 15))
    await store.save(state)

    loaded = await store.load(date(2026, 7, 15))
    assert loaded == state
    assert loaded is not None
    assert loaded.last_bar_time is None
    assert loaded.position is None


async def test_save_reraises_on_a_genuinely_failing_write(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = StateStore(db_path)
    await store.init_schema()

    # Replace the database file with a directory so the connection sqlite3
    # opens to perform the write can never succeed. A swallowed exception here
    # would look identical to a successful save from the caller's side, which
    # is exactly the silent corruption the design is meant to rule out.
    db_path.unlink()
    db_path.mkdir()

    with pytest.raises(sqlite3.OperationalError):
        await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=1))


async def test_concurrent_saves_to_the_same_session_date_do_not_corrupt_state(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()

    await asyncio.gather(
        *(
            store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=i))
            for i in range(20)
        )
    )

    loaded = await store.load(date(2026, 7, 15))
    assert loaded is not None
    assert loaded.trades_taken in range(20)


async def test_init_schema_is_safe_to_call_concurrently(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "nested" / "state.db")

    await asyncio.gather(*(store.init_schema() for _ in range(20)))
    await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=1))

    loaded = await store.load(date(2026, 7, 15))
    assert loaded is not None
    assert loaded.trades_taken == 1


class _GatedStore(StateStore):
    """StateStore whose write blocks a worker thread until released.

    Used only to prove save() truly hands the write off to another thread
    rather than running it inline. If the write ran on the event loop thread,
    entering the gate would freeze the loop that is supposed to deliver the
    release, and the test would fail instead of completing quickly — the same
    deterministic proof-by-deadlock RendezvousExecutor uses in test_router.py
    instead of a timing assertion.
    """

    def __init__(self, db_path: Path, entered: threading.Event, release: threading.Event) -> None:
        super().__init__(db_path)
        self._entered = entered
        self._release = release

    def _save_sync(self, session_date: str, payload: str) -> None:
        self._entered.set()
        assert self._release.wait(timeout=5.0), "worker thread was never released"
        super()._save_sync(session_date, payload)


async def test_save_runs_the_write_off_the_event_loop_thread(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    store = _GatedStore(tmp_path / "state.db", entered, release)
    await store.init_schema()

    save_task = asyncio.create_task(
        store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=7))
    )
    # Only possible if the event loop is still free to run this coroutine
    # while the worker thread sits inside the gate above.
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 5.0), timeout=5.0)
    release.set()
    await asyncio.wait_for(save_task, timeout=5.0)

    loaded = await store.load(date(2026, 7, 15))
    assert loaded is not None
    assert loaded.trades_taken == 7
