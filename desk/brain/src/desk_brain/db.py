"""Supabase (Postgres) access for the brain — async client, service role.

The brain writes only what spec §7 allows it to: chat_*, opinions,
observations, facts (proposed), watches, pings, notes (source=buddy),
opinions grading fields, sessions.day_read_json, and fills/trades via the
Tradovate auto-pull (same reconstruction contract as desk-web's CSV import).
"""

from __future__ import annotations

from supabase import AsyncClient, acreate_client

from .config import settings


async def make_db() -> AsyncClient:
    s = settings()
    return await acreate_client(s.supabase_url, s.supabase_service_role_key)
