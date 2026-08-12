from __future__ import annotations

import asyncio
import json
import threading
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nq_agent.clock import Clock
from nq_agent.models import require_utc


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return require_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        # mode="python" keeps Decimal, datetime and Enum as objects so they come
        # back through this encoder. mode="json" would render datetimes as RFC3339
        # "Z", which datetime.fromisoformat cannot parse before Python 3.11 and
        # which disagrees with the "+00:00" every other datetime here produces.
        return value.model_dump(mode="python")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__} for the journal")


RESERVED_KEYS = ("ts", "event")

# Every _append runs on a worker thread (see write's asyncio.to_thread), so any
# two journal writes in flight at once are genuinely concurrent. Append mode is
# NOT enough to make that safe: POSIX O_APPEND makes the seek-to-end and the
# write one atomic kernel operation, but the Windows CRT implements append by
# seeking to the end and then writing, as two steps. Two threads that seek to
# the same end offset then both write silently overwrite each other -- measured
# on Windows as 133 of 200 records surviving, with no exception raised and no
# corrupt line to notice it by, because each clobbered record was replaced
# whole. A journal that loses records without saying so is worse than no
# journal, so the write is serialised here rather than left to the platform.
#
# Module-level, not per-instance: the thing that must not be raced on is the
# file, and two Journal objects can point at the same directory. Writes are a
# handful of microseconds each and happen a few times per bar, so a single
# process-wide lock costs nothing measurable and needs no per-path bookkeeping.
_WRITE_LOCK = threading.Lock()


class Journal:
    """Append-only JSONL event log, one file per session date.

    Over-log rather than under-log. This is the debugging record and it later
    feeds the LLM filter's shadow-mode evaluation.
    """

    def __init__(self, journal_dir: Path, clock: Clock) -> None:
        self._dir = journal_dir
        self._clock = clock

    def path_for(self, session_date: date) -> Path:
        return self._dir / f"{session_date.isoformat()}.jsonl"

    def _append(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK, path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def write(self, event: str, session_date: date, **payload: Any) -> None:
        reserved = [key for key in RESERVED_KEYS if key in payload]
        if reserved:
            raise ValueError(
                f"payload keys {reserved} are reserved by the journal; "
                "rename them rather than overwriting record metadata"
            )
        record: dict[str, Any] = {"ts": self._clock.now().isoformat(), "event": event}
        record.update(payload)
        # allow_nan=False: NaN and Infinity are not valid JSON and would silently
        # produce a line that strict non-Python consumers cannot parse.
        line = json.dumps(record, default=_encode, allow_nan=False)
        await asyncio.to_thread(self._append, self.path_for(session_date), line)
