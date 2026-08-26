"""The Redis contract between State (writer) and everything else (readers).

Every value is a JSON document with a top-level "ts" (ISO UTC, when computed).
Readers never trust their own clock for staleness — they compare against the
heartbeat, which State refreshes every second while the engine is alive.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis

HEARTBEAT = "desk:heartbeat"  # {"ts": iso} refreshed ~1s
MARKET_STATE = "desk:market_state"  # last, session/on H-L, gap, vwap, value areas, cum delta
BARS_1M = "desk:bars:1m"  # list[bar], newest last, capped
BARS_5M = "desk:bars:5m"
BARS_15M = "desk:bars:15m"
LEVELS = "desk:levels"  # pdh/pdl, pwh/pwl, 4h swings, on h/l, gamma walls, statuses
REGIME = "desk:regime"  # gamma sign/magnitude, rv vs iv, day-type probs
POSITION = "desk:position"  # side, size, avg, upl, working orders, headroom
TAPE = "desk:tape"  # live tick-window read: speed, large prints, delta rate, absorption
DAILY = "desk:daily"  # bootstrap daily aggregates: OHLC rows, ATR, IB/ON range history
VBP_PREFIX = "desk:vbp:"  # per-minute volume-by-price hash, key suffix = epoch minute
VBP_TTL_S = 3 * 3600

EVENTS_CHANNEL = "desk:events"  # pub/sub: {"kind": "bar_1m"|"bar_5m", ...}
PINGS_CHANNEL = "desk:pings"  # pub/sub: ping rows, for the web SSE feed

STALE_AFTER_S = 5.0
OFFLINE_AFTER_S = 60.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def write_json(r: Redis, key: str, doc: dict[str, Any]) -> None:
    doc.setdefault("ts", now_iso())
    await r.set(key, json.dumps(doc, default=str))


async def read_json(r: Redis, key: str) -> dict[str, Any] | None:
    raw = await r.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


async def beat(r: Redis) -> None:
    await r.set(HEARTBEAT, json.dumps({"ts": now_iso()}))


async def heartbeat_age_s(r: Redis) -> float | None:
    """Seconds since the engine last beat, or None if it never has."""
    doc = await read_json(r, HEARTBEAT)
    if not doc or "ts" not in doc:
        return None
    try:
        then = datetime.fromisoformat(doc["ts"])
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()
