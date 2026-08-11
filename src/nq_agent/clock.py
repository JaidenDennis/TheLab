from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


def _require_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return ts


class Clock(ABC):
    """The only source of time in the system."""

    @abstractmethod
    def now(self) -> datetime:
        """Current time as timezone-aware UTC."""


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SimClock(Clock):
    """Deterministic clock. Advances only when fed a timestamp."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start).astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, ts: datetime) -> None:
        target = _require_aware(ts).astimezone(timezone.utc)
        if target < self._now:
            raise ValueError(f"clock cannot run backwards: {target} < {self._now}")
        self._now = target


class SessionCalendar:
    """Session windows in local exchange time, exposed as UTC comparisons."""

    def __init__(self, timezone: str, open_time: time, cutoff_time: time) -> None:
        self._zone = ZoneInfo(timezone)
        self._open = open_time
        self._cutoff = cutoff_time

    def session_date_for(self, ts: datetime) -> date:
        return _require_aware(ts).astimezone(self._zone).date()

    def _local_to_utc(self, session_date: date, at: time) -> datetime:
        local = datetime.combine(session_date, at, tzinfo=self._zone)
        return local.astimezone(timezone.utc)

    def open_utc(self, session_date: date) -> datetime:
        return self._local_to_utc(session_date, self._open)

    def cutoff_utc(self, session_date: date) -> datetime:
        return self._local_to_utc(session_date, self._cutoff)

    def is_session_open(self, ts: datetime) -> bool:
        ts = _require_aware(ts).astimezone(timezone.utc)
        session_date = self.session_date_for(ts)
        return self.open_utc(session_date) <= ts < self.cutoff_utc(session_date)

    def is_before_cutoff(self, ts: datetime) -> bool:
        ts = _require_aware(ts).astimezone(timezone.utc)
        return ts < self.cutoff_utc(self.session_date_for(ts))
