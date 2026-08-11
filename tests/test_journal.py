import asyncio
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from nq_agent.clock import SimClock
from nq_agent.journal import Journal
from nq_agent.models import Direction, Signal


async def test_write_creates_one_file_per_session_date(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    journal = Journal(tmp_path, clock)

    await journal.write("session_start", date(2026, 7, 15), symbol="NQ")
    await journal.write("session_start", date(2026, 7, 16), symbol="NQ")

    assert (tmp_path / "2026-07-15.jsonl").exists()
    assert (tmp_path / "2026-07-16.jsonl").exists()


async def test_records_carry_timestamp_and_event(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    journal = Journal(tmp_path, clock)

    await journal.write("risk_veto", date(2026, 7, 15), reason="KILL_SWITCH")

    line = (tmp_path / "2026-07-15.jsonl").read_text().strip()
    record = json.loads(line)
    assert record["ts"] == "2026-07-15T13:30:00+00:00"
    assert record["event"] == "risk_veto"
    assert record["reason"] == "KILL_SWITCH"


async def test_appends_rather_than_overwrites(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    journal = Journal(tmp_path, clock)

    await journal.write("a", date(2026, 7, 15))
    await journal.write("b", date(2026, 7, 15))

    lines = (tmp_path / "2026-07-15.jsonl").read_text().strip().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["a", "b"]


async def test_decimal_and_datetime_payloads_serialise(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    journal = Journal(tmp_path, clock)

    await journal.write(
        "signal_emitted",
        date(2026, 7, 15),
        entry_price=Decimal("20105.25"),
        at=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
    )

    record = json.loads((tmp_path / "2026-07-15.jsonl").read_text().strip())
    assert record["entry_price"] == "20105.25"
    assert record["at"] == "2026-07-15T13:31:00+00:00"


async def test_creates_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "journal"
    journal = Journal(target, SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)))
    await journal.write("session_start", date(2026, 7, 15))
    assert (target / "2026-07-15.jsonl").exists()


async def test_reserved_payload_key_is_rejected(tmp_path: Path) -> None:
    journal = Journal(tmp_path, SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)))

    with pytest.raises(ValueError, match="reserved"):
        await journal.write("tick", date(2026, 7, 15), ts="2099-01-01T00:00:00+00:00")

    assert not journal.path_for(date(2026, 7, 15)).exists()


async def test_naive_datetime_payload_is_rejected(tmp_path: Path) -> None:
    journal = Journal(tmp_path, SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)))

    with pytest.raises(ValueError, match="timezone-aware"):
        await journal.write("bad", date(2026, 7, 15), at=datetime(2026, 7, 15, 9, 0))


async def test_nan_and_infinity_are_rejected(tmp_path: Path) -> None:
    journal = Journal(tmp_path, SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)))

    with pytest.raises(ValueError):
        await journal.write("bad", date(2026, 7, 15), value=float("nan"))


async def test_unserialisable_payload_raises_before_touching_disk(tmp_path: Path) -> None:
    journal = Journal(tmp_path, SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)))

    with pytest.raises(TypeError, match="cannot serialise"):
        await journal.write("bad", date(2026, 7, 15), value=object())

    assert not journal.path_for(date(2026, 7, 15)).exists()


async def test_nested_model_datetimes_use_the_same_offset_format(tmp_path: Path) -> None:
    """A nested model must not render datetimes as 'Z'.

    Python 3.10's datetime.fromisoformat cannot parse a trailing 'Z', so a
    journal mixing the two formats is unreadable by its own tooling.
    """
    journal = Journal(tmp_path, SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)))
    signal = Signal(
        timestamp=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        symbol="NQ",
        direction=Direction.LONG,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
        quantity=1,
        reason="test",
    )

    await journal.write("signal_emitted", date(2026, 7, 15), signal=signal)

    record = json.loads(journal.path_for(date(2026, 7, 15)).read_text().strip())
    nested = record["signal"]["timestamp"]
    assert nested == "2026-07-15T13:31:00+00:00"
    assert datetime.fromisoformat(nested) == signal.timestamp
    assert record["signal"]["entry_price"] == "20100"
    assert record["signal"]["direction"] == "LONG"


async def test_concurrent_writes_do_not_interleave_or_drop_records(tmp_path: Path) -> None:
    journal = Journal(tmp_path, SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)))
    session = date(2026, 7, 15)

    await asyncio.gather(
        *(journal.write("bulk", session, index=i, filler="x" * 4000) for i in range(200))
    )

    lines = journal.path_for(session).read_text().strip().splitlines()
    assert len(lines) == 200
    assert sorted(json.loads(line)["index"] for line in lines) == list(range(200))
