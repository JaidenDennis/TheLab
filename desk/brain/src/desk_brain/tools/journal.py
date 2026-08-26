"""Journal tools: read trades/notes (direct Postgres query) and write buddy notes."""

from __future__ import annotations

from typing import Any

from . import ToolContext, tool


@tool(
    "journal",
    {
        "description": (
            "Query the trade journal: filters over direction, outcome, date range, session "
            "bucket, and facet tag; returns matching trades with their notes plus aggregate "
            "stats. Every aggregate includes n — treat n < 20 as insufficient sample."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["long", "short"]},
                "outcome": {"type": "string", "enum": ["win", "loss"]},
                "date_from": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                "tag": {"type": "string", "description": "facet tag label, e.g. 'sweep + reversal'"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
    },
)
async def journal(ctx: ToolContext, args: dict[str, Any]) -> Any:
    q = ctx.db.table("trades").select(
        "id, session_date, contract, direction, size, avg_entry, avg_exit, net_pnl, entry_at, exit_at, narrative, "
        "trade_tags(tags(facet, label)), notes(body, source, captured_at)"
    )
    if args.get("direction"):
        q = q.eq("direction", args["direction"])
    if args.get("date_from"):
        q = q.gte("session_date", args["date_from"])
    if args.get("date_to"):
        q = q.lte("session_date", args["date_to"])
    if args.get("outcome") == "win":
        q = q.gt("net_pnl", 0)
    elif args.get("outcome") == "loss":
        q = q.lte("net_pnl", 0)
    limit = int(args.get("limit", 20))
    res = await q.order("entry_at", desc=True).limit(200).execute()
    rows = res.data or []

    if args.get("tag"):
        want = args["tag"].strip().lower()
        rows = [
            r
            for r in rows
            if any((tt.get("tags") or {}).get("label", "").lower() == want for tt in r.get("trade_tags") or [])
        ]

    n = len(rows)
    pnls = [float(r["net_pnl"]) for r in rows]
    stats = {
        "n": n,
        "sufficient_sample": n >= 20,
        "net_total": round(sum(pnls), 2),
        "win_rate": round(sum(1 for p in pnls if p > 0) / n, 3) if n else None,
        "avg_net": round(sum(pnls) / n, 2) if n else None,
    }
    trades = [
        {
            "session_date": r["session_date"],
            "contract": r["contract"],
            "direction": r["direction"],
            "size": r["size"],
            "net_pnl": float(r["net_pnl"]),
            "entry_at": r["entry_at"],
            "tags": [
                (tt.get("tags") or {}).get("label")
                for tt in r.get("trade_tags") or []
                if tt.get("tags")
            ],
            "notes": [
                {"source": nt["source"], "body": nt["body"]}
                for nt in (r.get("notes") or [])[:5]
            ],
            "narrative": (r.get("narrative") or "")[:500] or None,
        }
        for r in rows[:limit]
    ]
    return {"stats": stats, "trades": trades}


@tool(
    "note",
    {
        "description": "Write a note into the journal with source=buddy (never merged with Jay's own notes in stats).",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
)
async def note(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from .. import redis_keys as rk

    res = (
        await ctx.db.table("notes")
        .insert(
            {
                "body": args["text"].strip(),
                "captured_at": rk.now_iso(),
                "source": "buddy",
                "tags": args.get("tags") or [],
            }
        )
        .execute()
    )
    return {"id": (res.data or [{}])[0].get("id")}
