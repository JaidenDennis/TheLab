from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from nq_agent.clock import RealClock, SessionCalendar, SimClock

NY = ZoneInfo("America/New_York")


def calendar() -> SessionCalendar:
    return SessionCalendar("America/New_York", time(9, 30), time(16, 30))


def test_real_clock_returns_aware_utc() -> None:
    now = RealClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_sim_clock_only_moves_when_advanced() -> None:
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    clock = SimClock(start)
    assert clock.now() == start
    assert clock.now() == start
    clock.advance_to(datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc))
    assert clock.now() == datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc)


def test_sim_clock_refuses_to_go_backwards() -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))


def test_session_date_uses_new_york_calendar_date() -> None:
    # 01:00 UTC on the 16th is 21:00 on the 15th in New York.
    ts = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)
    assert calendar().session_date_for(ts) == date(2026, 7, 15)


def test_session_open_and_cutoff_during_edt() -> None:
    cal = calendar()
    # July is EDT, UTC-4. 09:30 ET == 13:30 UTC.
    assert cal.is_session_open(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)) is True
    assert cal.is_session_open(datetime(2026, 7, 15, 13, 29, tzinfo=timezone.utc)) is False
    assert cal.is_before_cutoff(datetime(2026, 7, 15, 20, 29, tzinfo=timezone.utc)) is True
    assert cal.is_before_cutoff(datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)) is False


def test_session_open_and_cutoff_during_est() -> None:
    cal = calendar()
    # January is EST, UTC-5. 09:30 ET == 14:30 UTC.
    assert cal.is_session_open(datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)) is True
    assert cal.is_session_open(datetime(2026, 1, 15, 14, 29, tzinfo=timezone.utc)) is False
    assert cal.is_before_cutoff(datetime(2026, 1, 15, 21, 29, tzinfo=timezone.utc)) is True
    assert cal.is_before_cutoff(datetime(2026, 1, 15, 21, 30, tzinfo=timezone.utc)) is False


def test_cutoff_utc_shifts_across_spring_forward() -> None:
    cal = calendar()
    # DST begins 2026-03-08. The day before is EST, the day after is EDT.
    assert cal.cutoff_utc(date(2026, 3, 7)) == datetime(2026, 3, 7, 21, 30, tzinfo=timezone.utc)
    assert cal.cutoff_utc(date(2026, 3, 9)) == datetime(2026, 3, 9, 20, 30, tzinfo=timezone.utc)


def test_cutoff_utc_shifts_across_fall_back() -> None:
    cal = calendar()
    # DST ends 2026-11-01. The day before is EDT, the day after is EST.
    assert cal.cutoff_utc(date(2026, 10, 31)) == datetime(2026, 10, 31, 20, 30, tzinfo=timezone.utc)
    assert cal.cutoff_utc(date(2026, 11, 2)) == datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)


def test_spring_forward_day_itself_is_edt_by_session_open() -> None:
    cal = calendar()
    # The 02:00 -> 03:00 jump happens before 09:30, so 2026-03-08 trades on EDT.
    assert cal.cutoff_utc(date(2026, 3, 8)) == datetime(2026, 3, 8, 20, 30, tzinfo=timezone.utc)


def test_fall_back_day_itself_is_est_by_session_open() -> None:
    cal = calendar()
    # The 02:00 -> 01:00 repeat happens before 09:30, so 2026-11-01 trades on EST.
    assert cal.cutoff_utc(date(2026, 11, 1)) == datetime(2026, 11, 1, 21, 30, tzinfo=timezone.utc)


def test_session_date_is_stable_across_the_fall_back_repeated_hour() -> None:
    cal = calendar()
    # 05:30 UTC on 2026-11-01 is 01:30 EDT; 06:30 UTC is 01:30 EST. Same NY date.
    edt_instant = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    est_instant = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
    assert cal.session_date_for(edt_instant) == date(2026, 11, 1)
    assert cal.session_date_for(est_instant) == date(2026, 11, 1)


def test_naive_input_is_rejected() -> None:
    cal = calendar()
    with pytest.raises(ValueError, match="timezone-aware"):
        cal.session_date_for(datetime(2026, 7, 15, 13, 30))
