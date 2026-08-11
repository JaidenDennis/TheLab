import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SimClock
from nq_agent.journal import Journal


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
