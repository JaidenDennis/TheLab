"""Buddy tool layer (spec §13).

Every tool returns {"ok": True, "data": ..., "as_of": iso, "stale": bool}
or {"ok": False, "reason": str}. `stale` is True when the engine heartbeat
is older than 5 s. Tools are pure reads except `note`, which writes a
journal note with source="buddy".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis
from supabase import AsyncClient

from .. import redis_keys as rk
from ..config import Settings
from ..factors import Factor

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    redis: Redis
    db: AsyncClient
    settings: Settings
    factors: dict[str, Factor]
    params: dict[str, Any] | None = None  # desk/signals.yaml, loaded at startup


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]

_REGISTRY: dict[str, tuple[dict, Handler]] = {}


def tool(name: str, schema: dict) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _REGISTRY[name] = (schema, fn)
        return fn

    return deco


def anthropic_tools() -> list[dict]:
    """Tool definitions in Anthropic Messages API shape."""
    return [
        {"name": name, "description": schema["description"], "input_schema": schema["input_schema"]}
        for name, (schema, _) in _REGISTRY.items()
    ]


def tool_names() -> list[str]:
    return list(_REGISTRY)


async def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    entry = _REGISTRY.get(name)
    if entry is None:
        return {"ok": False, "reason": f"unknown tool {name!r}"}
    _, handler = entry
    try:
        data = await handler(ctx, args or {})
    except Exception as e:  # noqa: BLE001 — tool failure must surface, not crash the loop
        log.exception("tool %s failed", name)
        return {"ok": False, "reason": f"{name} failed: {e}"}
    age = await rk.heartbeat_age_s(ctx.redis)
    stale = age is None or age > rk.STALE_AFTER_S
    return {"ok": True, "data": data, "as_of": rk.now_iso(), "stale": stale}


# Import modules for their @tool registration side effects.
from . import market  # noqa: E402,F401
from . import position  # noqa: E402,F401
from . import journal  # noqa: E402,F401
from . import research  # noqa: E402,F401
from . import calendar  # noqa: E402,F401
from . import reads  # noqa: E402,F401
