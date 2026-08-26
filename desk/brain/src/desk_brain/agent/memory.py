"""Three-tier memory (spec §15): session transcript + summary, durable facts,
and observations surfaced by relevance to the current question.

Observation relevance is lexical (keyword overlap over the most recent 200
rows). The observations table has a pgvector column for a later embedding
upgrade; nothing else changes when that lands.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from supabase import AsyncClient

ET = ZoneInfo("America/New_York")
SUMMARY_EVERY_TURNS = 20
_WORD = re.compile(r"[a-z0-9]{3,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class Memory:
    def __init__(self, db: AsyncClient):
        self._db = db

    async def chat_session(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        res = (
            await self._db.table("chat_sessions")
            .select("*")
            .eq("session_date", today)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
        created = await self._db.table("chat_sessions").insert({"session_date": today}).execute()
        return created.data[0]

    async def recent_turns(self, chat_session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        res = (
            await self._db.table("chat_messages")
            .select("role, content, ts")
            .eq("chat_session_id", chat_session_id)
            .order("ts", desc=True)
            .limit(limit)
            .execute()
        )
        return list(reversed(res.data or []))

    async def turn_count(self, chat_session_id: str) -> int:
        res = (
            await self._db.table("chat_messages")
            .select("id", count="exact")
            .eq("chat_session_id", chat_session_id)
            .execute()
        )
        return res.count or 0

    async def append(self, chat_session_id: str, role: str, content: str, tool_calls: Any = None) -> None:
        await self._db.table("chat_messages").insert(
            {"chat_session_id": chat_session_id, "role": role, "content": content, "tool_calls_json": tool_calls}
        ).execute()

    async def set_summary(self, chat_session_id: str, summary: str) -> None:
        await self._db.table("chat_sessions").update(
            {"summary": summary, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", chat_session_id).execute()

    async def active_facts(self) -> list[str]:
        res = await self._db.table("facts").select("text").eq("active", True).order("created_at").execute()
        return [r["text"] for r in res.data or []]

    async def propose_fact(self, text: str, source: str = "agent_proposed") -> str:
        res = await self._db.table("facts").insert({"text": text, "source": source, "active": False}).execute()
        return res.data[0]["id"]

    async def confirm_latest_fact(self) -> str | None:
        res = (
            await self._db.table("facts")
            .select("id, text")
            .eq("active", False)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        row = res.data[0]
        await self._db.table("facts").update({"active": True}).eq("id", row["id"]).execute()
        return row["text"]

    async def relevant_observations(self, question: str, k: int = 5) -> list[str]:
        res = (
            await self._db.table("observations")
            .select("text, ts")
            .order("ts", desc=True)
            .limit(200)
            .execute()
        )
        q = _words(question)
        if not q:
            return []
        scored = []
        for row in res.data or []:
            overlap = len(q & _words(row["text"]))
            if overlap > 0:
                scored.append((overlap, row["text"]))
        scored.sort(key=lambda t: -t[0])
        return [t for _, t in scored[:k]]

    async def add_observation(self, text: str, trade_ids: list[str] | None = None) -> None:
        await self._db.table("observations").insert({"text": text, "trade_ids": trade_ids or []}).execute()

    async def today_context(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        session = (
            await self._db.table("sessions").select("*").eq("session_date", today).maybe_single().execute()
        )
        checklists = (
            await self._db.table("checklist_entries")
            .select("trade_number, htf_bias, htf_bias_overridden, amd_phase, conviction, entry_confirmation, rule_violations, created_at")
            .eq("session_date", today)
            .order("created_at")
            .execute()
        )
        return {"session": getattr(session, "data", None), "checklists": checklists.data or []}

    async def open_trade_id(self) -> str | None:
        """Most recent trade whose exit is within the last 2 minutes counts as
        'open' only via the position tool; journal trades are closed round-trips,
        so the open-trade link uses today's last trade if the broker still shows
        a position — resolved by the caller. Returns today's latest trade id."""
        today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        res = (
            await self._db.table("trades")
            .select("id")
            .eq("session_date", today)
            .order("exit_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0]["id"] if res.data else None
