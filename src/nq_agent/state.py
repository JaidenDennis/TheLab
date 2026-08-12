from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
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

    # `with conn:` commits or rolls back the transaction -- it does NOT close
    # the connection, which is why every one of these pairs it with closing().
    # Without that, the handle lives until the garbage collector reaches it,
    # and the connection opened by the most recent to_thread call stays alive
    # in that now-idle worker thread until the thread happens to run something
    # else. On POSIX an open handle costs nothing (unlink works regardless), so
    # this read as benign there; on Windows an open handle locks the file, so
    # state.db could not be deleted, renamed or swapped while the agent ran.
    # Closing a database handle should not depend on GC timing on any platform.

    def _init_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(SCHEMA)

    def _save_sync(self, session_date: str, payload: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO session_state (session_date, payload) VALUES (?, ?) "
                "ON CONFLICT(session_date) DO UPDATE SET payload = excluded.payload",
                (session_date, payload),
            )

    def _load_sync(self, session_date: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload FROM session_state WHERE session_date = ?", (session_date,)
            ).fetchone()
        return str(row[0]) if row else None

    def _load_latest_sync(self) -> str | None:
        # session_date is ISO text, so lexicographic DESC is chronological.
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload FROM session_state ORDER BY session_date DESC LIMIT 1"
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

    async def load_latest(self) -> SessionState | None:
        """The most recently persisted session, whatever its date.

        A replay resumes from here: its anchor is the fixture's first tick,
        which names the file's FIRST session no matter how far a killed run
        had actually got. A live process never uses this -- its anchor is
        `now`, which correctly names the session being resumed.
        """
        payload = await asyncio.to_thread(self._load_latest_sync)
        if payload is None:
            return None
        return SessionState.model_validate_json(payload)
