from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nq_agent.clock import Clock


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__} for the journal")


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
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def write(self, event: str, session_date: date, **payload: Any) -> None:
        record: dict[str, Any] = {"ts": self._clock.now().isoformat(), "event": event}
        record.update(payload)
        line = json.dumps(record, default=_encode)
        await asyncio.to_thread(self._append, self.path_for(session_date), line)
