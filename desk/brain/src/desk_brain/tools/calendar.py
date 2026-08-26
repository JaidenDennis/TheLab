"""Econ calendar tool. Backed by the calendar_events table (migration 0003);
populated manually or by pasting a week of events in Settings — no scraping."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import ToolContext, tool

ET = ZoneInfo("America/New_York")


@tool(
    "calendar",
    {
        "description": "Economic-calendar events for a date (default today, ET): time, name, impact.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD; defaults to today ET"}},
            "additionalProperties": False,
        },
    },
)
async def calendar(ctx: ToolContext, args: dict[str, Any]) -> Any:
    date = args.get("date") or datetime.now(timezone.utc).astimezone(ET).date().isoformat()
    res = (
        await ctx.db.table("calendar_events")
        .select("event_date, event_time_et, name, impact")
        .eq("event_date", date)
        .order("event_time_et")
        .execute()
    )
    events = res.data or []
    return {
        "date": date,
        "events": events,
        "note": None if events else "no events recorded for this date — the table may simply be unpopulated",
    }
