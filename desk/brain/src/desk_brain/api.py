"""Internal HTTP API (spec §3): POST /chat (SSE), GET /pings (SSE), GET /status.

Lives on Render's private network; authenticated by the shared secret.
Commands are handled here, before any model call (spec §14).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from . import redis_keys as rk
from .agent.loop import BuddyAgent
from .agent.memory import Memory
from .tools import ToolContext
from .watcher import MUTE_KEY

log = logging.getLogger(__name__)


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def build_app(agent: BuddyAgent, memory: Memory, ctx: ToolContext) -> FastAPI:
    app = FastAPI(title="desk-brain", docs_url=None, redoc_url=None, openapi_url=None)
    redis: Redis = ctx.redis
    secret = ctx.settings.brain_shared_secret

    async def auth(request: Request) -> None:
        got = request.headers.get("x-brain-secret", "")
        if not secret or got != secret:
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/status", dependencies=[Depends(auth)])
    async def status() -> dict[str, Any]:
        age = await rk.heartbeat_age_s(redis)
        pos = await rk.read_json(redis, rk.POSITION)
        market = await rk.read_json(redis, rk.MARKET_STATE)
        muted_until = await redis.get(MUTE_KEY)
        return {
            "heartbeat_age_s": age,
            "engine": "ok" if age is not None and age <= rk.STALE_AFTER_S else "stale" if age is not None and age <= rk.OFFLINE_AFTER_S else "offline",
            "tradovate_connected": bool(pos and pos.get("connected")),
            "last": (market or {}).get("last"),
            "positions": (pos or {}).get("positions", []),
            "working_orders": (pos or {}).get("working_orders", []),
            "muted_until": muted_until if isinstance(muted_until, (str, type(None))) else muted_until.decode(),
        }

    @app.post("/chat", dependencies=[Depends(auth)])
    async def chat(request: Request) -> StreamingResponse:
        body = await request.json()
        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="empty message")

        async def stream() -> AsyncIterator[str]:
            if message.startswith("/"):
                reply = await handle_command(message)
                yield sse({"kind": "final", "text": reply, "tools_used": [], "stale": False, "command": True})
                return
            try:
                async for event in agent.chat(message):
                    yield sse(event)
            except Exception as e:  # noqa: BLE001 — surface, don't drop the stream
                log.exception("chat turn failed")
                yield sse({"kind": "final", "text": f"buddy error: {e}", "error": True})

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def handle_command(message: str) -> str:
        parts = message.split(maxsplit=1)
        cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")

        if cmd == "/watch":
            m = re.match(r"^\d+(\.\d+)?$", arg)
            if not m:
                return "usage: /watch <price>"
            await ctx.db.table("watches").insert({"price": float(arg)}).execute()
            return f"Watching {arg} — I'll ping on Q90+ flow within 3 pts."

        if cmd == "/unwatch":
            q = ctx.db.table("watches").update({"active": False}).eq("active", True)
            if arg:
                try:
                    q = q.eq("price", float(arg))
                except ValueError:
                    return "usage: /unwatch [<price>]"
            res = await q.execute()
            n = len(res.data or [])
            return f"Cleared {n} watch{'es' if n != 1 else ''}."

        if cmd == "/mute":
            try:
                minutes = int(arg or "30")
            except ValueError:
                return "usage: /mute <minutes>"
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            await redis.set(MUTE_KEY, until.isoformat())
            return f"Muted all pings for {minutes} min (until {until.astimezone().strftime('%H:%M')})."

        if cmd == "/remember":
            if not arg:
                return "usage: /remember <text>"
            await memory.propose_fact(arg, source="user")
            return f"Proposed fact (inactive until /confirm): “{arg}”"

        if cmd == "/confirm":
            text = await memory.confirm_latest_fact()
            return f"Confirmed: “{text}”" if text else "Nothing pending to confirm."

        if cmd == "/note":
            if not arg:
                return "usage: /note <text>"
            from .tools import run_tool

            result = await run_tool(ctx, "note", {"text": arg})
            return "Noted." if result.get("ok") else f"Note failed: {result.get('reason')}"

        if cmd == "/status":
            s = await status()
            return (
                f"engine: {s['engine']} (heartbeat {s['heartbeat_age_s'] and round(s['heartbeat_age_s'], 1)}s) · "
                f"tradovate: {'connected' if s['tradovate_connected'] else 'down'} · last {s['last']} · "
                f"{'muted until ' + s['muted_until'] if s['muted_until'] else 'not muted'}"
            )

        return f"Unknown command {cmd}. Commands: /watch /unwatch /mute /remember /confirm /note /status"

    @app.get("/pings", dependencies=[Depends(auth)])
    async def pings() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            backlog = (
                await ctx.db.table("pings").select("*").order("ts", desc=True).limit(20).execute()
            )
            for row in reversed(backlog.data or []):
                yield sse({"kind": "backlog", "ping": row})
            pubsub = redis.pubsub()
            await pubsub.subscribe(rk.PINGS_CHANNEL)
            try:
                while True:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                    if msg is None:
                        yield ": keepalive\n\n"
                        continue
                    data = msg["data"]
                    yield sse({"kind": "ping", "ping": json.loads(data if isinstance(data, str) else data.decode())})
            finally:
                await pubsub.unsubscribe(rk.PINGS_CHANNEL)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
