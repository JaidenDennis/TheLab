from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.journal import Journal
from nq_agent.models import Bar, Direction, Position, SignalIntent
from nq_agent.session import SessionManager
from nq_agent.strategy.always import AlwaysStrategy

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 New York
CUTOFF = datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)  # 16:30 New York


def calendar() -> SessionCalendar:
    return SessionCalendar("America/New_York", time(9, 30), time(16, 30))


def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path, SimClock(OPEN))


def bar_closing_at(close_time: datetime, close: str = "20100") -> Bar:
    return Bar(
        symbol="NQ",
        timeframe="1m",
        open_time=close_time - timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
    )


def open_position() -> Position:
    return Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=2,
        entry_price=Decimal("20100"),
        entry_time=OPEN,
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
    )


def short_position() -> Position:
    """Deliberately unlike open_position(): different direction and quantity.

    A FLATTEN implementation that hardcoded LONG/2 instead of reading the
    position's own fields would still pass every test built only on
    open_position(). This pins that it does not.
    """
    return Position(
        symbol="NQ",
        direction=Direction.SHORT,
        quantity=5,
        entry_price=Decimal("20100"),
        entry_time=OPEN,
        stop_price=Decimal("20110"),
        target_price=Decimal("20080"),
    )


async def test_first_bar_starts_the_session(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    manager = SessionManager(strategy, calendar(), journal(tmp_path))

    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)

    assert manager.current_session_date == date(2026, 7, 15)
    assert '"event": "session_start"' in (tmp_path / "2026-07-15.jsonl").read_text()


async def test_session_rollover_ends_the_old_and_starts_the_new(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    manager = SessionManager(strategy, calendar(), journal(tmp_path))

    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)
    await manager.on_bar(bar_closing_at(OPEN + timedelta(days=1, minutes=1)), None)

    assert manager.current_session_date == date(2026, 7, 16)
    assert '"event": "session_end"' in (tmp_path / "2026-07-15.jsonl").read_text()
    assert '"event": "session_start"' in (tmp_path / "2026-07-16.jsonl").read_text()


async def test_no_flatten_before_cutoff(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))
    result = await manager.on_bar(bar_closing_at(CUTOFF - timedelta(minutes=1)), open_position())
    assert result is None


async def test_no_flatten_when_flat(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))
    assert await manager.on_bar(bar_closing_at(CUTOFF), None) is None


async def test_flatten_emitted_at_cutoff_with_an_open_position(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))

    signal = await manager.on_bar(bar_closing_at(CUTOFF), open_position())

    assert signal is not None
    assert signal.intent is SignalIntent.FLATTEN
    assert signal.direction is Direction.LONG
    assert signal.quantity == 2
    assert signal.entry_price is None
    assert signal.timestamp == CUTOFF


async def test_flatten_is_emitted_only_once_per_session(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))

    first = await manager.on_bar(bar_closing_at(CUTOFF), open_position())
    second = await manager.on_bar(bar_closing_at(CUTOFF + timedelta(minutes=1)), open_position())

    assert first is not None
    assert second is None


async def test_flatten_rearms_on_the_next_session(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))
    await manager.on_bar(bar_closing_at(CUTOFF), open_position())

    next_day_cutoff = CUTOFF + timedelta(days=1)
    signal = await manager.on_bar(bar_closing_at(next_day_cutoff), open_position())
    assert signal is not None


async def test_end_session_closes_the_open_session(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    manager = SessionManager(strategy, calendar(), journal(tmp_path))
    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)

    await manager.end_session()

    assert manager.current_session_date is None
    assert '"event": "session_end"' in (tmp_path / "2026-07-15.jsonl").read_text()


async def test_strategy_lifecycle_hooks_fire(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    manager = SessionManager(strategy, calendar(), journal(tmp_path))

    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)
    strategy.restore_state({"fired": True})
    await manager.on_bar(bar_closing_at(OPEN + timedelta(days=1, minutes=1)), None)

    # on_session_start reset the flag, so the strategy is armed again.
    assert strategy.get_state() == {"fired": False}


# --- Self-review additions: gaps found by mutation-testing the brief's own suite.
# See task-12-report.md for which mutation each one kills.


async def test_end_session_is_a_no_op_with_nothing_to_end(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))

    await manager.end_session()

    assert manager.current_session_date is None
    assert not (tmp_path / "2026-07-15.jsonl").exists()


async def test_end_session_is_idempotent(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))
    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)

    await manager.end_session()
    await manager.end_session()

    text = (tmp_path / "2026-07-15.jsonl").read_text()
    assert text.count('"event": "session_end"') == 1


async def test_flatten_uses_the_positions_direction_and_quantity(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))

    signal = await manager.on_bar(bar_closing_at(CUTOFF), short_position())

    assert signal is not None
    assert signal.direction is Direction.SHORT
    assert signal.quantity == 5


async def test_flatten_emitted_for_a_bar_closing_after_cutoff(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))
    after_cutoff = bar_closing_at(CUTOFF + timedelta(minutes=5))

    signal = await manager.on_bar(after_cutoff, open_position())

    assert signal is not None
    assert signal.intent is SignalIntent.FLATTEN


# --- Required addition: resume handling ---
#
# Strategy.on_session_start unconditionally clears per-session state (see its
# docstring; AlwaysStrategy._fired is the concrete example). A caller resuming a
# crashed mid-session run restores strategy state before the engine loop starts,
# so on_bar must not call on_session_start for the session being resumed into -
# doing so would silently wipe the restore on the very first bar.


async def test_resume_into_the_matching_session_skips_on_session_start(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    strategy.restore_state({"fired": True})
    manager = SessionManager(
        strategy, calendar(), journal(tmp_path), resumed_session_date=date(2026, 7, 15)
    )

    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)

    assert manager.current_session_date == date(2026, 7, 15)
    assert strategy.get_state() == {"fired": True}
    text = (tmp_path / "2026-07-15.jsonl").read_text()
    assert '"event": "session_resumed"' in text
    assert '"event": "session_start"' not in text


async def test_resume_date_mismatch_starts_the_session_normally(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    strategy.restore_state({"fired": True})
    manager = SessionManager(
        strategy, calendar(), journal(tmp_path), resumed_session_date=date(2026, 7, 14)
    )

    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)

    assert manager.current_session_date == date(2026, 7, 15)
    assert strategy.get_state() == {"fired": False}
    text = (tmp_path / "2026-07-15.jsonl").read_text()
    assert '"event": "session_start"' in text
    assert '"event": "session_resumed"' not in text


async def test_rollover_after_a_resume_ends_then_starts_normally(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    manager = SessionManager(
        strategy, calendar(), journal(tmp_path), resumed_session_date=date(2026, 7, 15)
    )
    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)

    await manager.on_bar(bar_closing_at(OPEN + timedelta(days=1, minutes=1)), None)

    assert manager.current_session_date == date(2026, 7, 16)
    assert '"event": "session_end"' in (tmp_path / "2026-07-15.jsonl").read_text()
    assert '"event": "session_start"' in (tmp_path / "2026-07-16.jsonl").read_text()


async def test_resume_date_matching_a_later_session_is_not_treated_as_a_resume(
    tmp_path: Path,
) -> None:
    """resumed_session_date only ever applies to the manager's first bar.

    A later session that happens to land on that same calendar date (e.g. a
    one-year cycle, or just an unlucky coincidence) is not the session that
    crashed, so it must start normally, on_session_start included.
    """
    strategy = AlwaysStrategy()
    manager = SessionManager(
        strategy, calendar(), journal(tmp_path), resumed_session_date=date(2026, 7, 16)
    )
    # First bar is July 15: a mismatch against the resumed date, starts normally.
    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)

    # Rollover lands on July 16 -- the resumed_session_date -- but this is not
    # the manager's first bar, so it must be a normal start, not an adoption.
    strategy.restore_state({"fired": True})
    await manager.on_bar(bar_closing_at(OPEN + timedelta(days=1, minutes=1)), None)

    assert manager.current_session_date == date(2026, 7, 16)
    assert strategy.get_state() == {"fired": False}  # on_session_start reset it
    text = (tmp_path / "2026-07-16.jsonl").read_text()
    assert '"event": "session_start"' in text
    assert '"event": "session_resumed"' not in text


async def test_resume_past_cutoff_still_flattens_on_the_first_bar(tmp_path: Path) -> None:
    """The scenario the addition exists for: crash after cutoff, restart, flatten still fires."""
    strategy = AlwaysStrategy()
    strategy.restore_state({"fired": True})
    manager = SessionManager(
        strategy, calendar(), journal(tmp_path), resumed_session_date=date(2026, 7, 15)
    )

    signal = await manager.on_bar(bar_closing_at(CUTOFF), open_position())

    assert signal is not None
    assert signal.intent is SignalIntent.FLATTEN
    assert strategy.get_state() == {"fired": True}
