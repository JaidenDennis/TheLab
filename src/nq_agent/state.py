from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from pathlib import Path

from nq_agent.models import SessionState

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_state (
    session_date TEXT PRIMARY KEY,
    payload      TEXT NOT NULL
)
"""


class StateStore:
    """SessionState persistence, written after every state transition.

    SQLite through the stdlib driver on a worker thread. Two writes a day does
    not justify an aiosqlite dependency, and a failed write is re-raised rather
    than swallowed — silent state corruption is worse than a crash.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _init_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(SCHEMA)

    def _save_sync(self, session_date: str, payload: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO session_state (session_date, payload) VALUES (?, ?) "
                "ON CONFLICT(session_date) DO UPDATE SET payload = excluded.payload",
                (session_date, payload),
            )

    def _load_sync(self, session_date: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM session_state WHERE session_date = ?", (session_date,)
            ).fetchone()
        return str(row[0]) if row else None

    async def init_schema(self) -> None:
        await asyncio.to_thread(self._init_sync)

    async def save(self, state: SessionState) -> None:
        await asyncio.to_thread(
            self._save_sync, state.session_date.isoformat(), state.model_dump_json()
        )

    async def load(self, session_date: date) -> SessionState | None:
        payload = await asyncio.to_thread(self._load_sync, session_date.isoformat())
        if payload is None:
            return None
        return SessionState.model_validate_json(payload)
