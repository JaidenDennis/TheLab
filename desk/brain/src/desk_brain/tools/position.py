"""Position tool: live Tradovate position + risk-governor headroom."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import redis_keys as rk
from . import ToolContext, tool

ET = ZoneInfo("America/New_York")
MAX_TRADES_PER_DAY = 2
SHUTOFF_ET = (15, 55)  # discretionary hard-stop per Jay's rules


@tool(
    "position",
    {
        "description": (
            "Live account state: open position (side, size, avg price, unrealized P&L), "
            "working orders, and risk-governor headroom (trades left of the 2/day rule, "
            "daily loss remaining if the broker exposes an auto-liq limit, minutes to the "
            "15:55 ET shutoff)."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)
async def position(ctx: ToolContext, args: dict[str, Any]) -> Any:
    doc = await rk.read_json(ctx.redis, rk.POSITION)
    if doc is None or not doc.get("connected", False):
        raise RuntimeError("position unavailable — Tradovate sync not connected")

    now_et = datetime.now(timezone.utc).astimezone(ET)
    today = now_et.date().isoformat()

    res = (
        await ctx.db.table("checklist_entries")
        .select("id", count="exact")
        .eq("session_date", today)
        .execute()
    )
    trades_taken = res.count or 0

    shutoff = now_et.replace(hour=SHUTOFF_ET[0], minute=SHUTOFF_ET[1], second=0, microsecond=0)
    minutes_to_shutoff = max(0, int((shutoff - now_et).total_seconds() // 60)) if now_et < shutoff else 0

    loss_left = None
    auto_liq = doc.get("auto_liq") or {}
    daily_loss_limit = auto_liq.get("dailyLossAutoLiq") or auto_liq.get("dailyLossPercentageAutoLiq")
    cash = doc.get("cash") or {}
    realized = next(iter(cash.values()), {}).get("amount")
    if isinstance(daily_loss_limit, (int, float)) and daily_loss_limit > 0:
        loss_left = {"daily_loss_limit": daily_loss_limit, "note": "from broker auto-liq settings"}

    return {
        "positions": doc.get("positions", []),
        "working_orders": doc.get("working_orders", []),
        "headroom": {
            "trades_left": max(0, MAX_TRADES_PER_DAY - trades_taken),
            "loss_left": loss_left,
            "minutes_to_shutoff": minutes_to_shutoff,
            "cash_balance": realized,
        },
        "position_ts": doc.get("ts"),
    }
