"""Watcher (spec §17): fixed trigger list, evaluated deterministically on
engine events and a 30s clock — no model call decides whether to ping.

Rate limit: 1 ping per trigger type per 10 min; global 8 per session
excluding pre-open and close; overflow folds into the next ping as a count.
/mute silences everything until the mute expires.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from supabase import AsyncClient

from . import redis_keys as rk
from .tools import ToolContext, run_tool

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MUTE_KEY = "desk:mute_until"
LAST_DAY_READ_KEY = "desk:watcher:last_day_read_lean"

PER_TRIGGER_COOLDOWN_S = 600
GLOBAL_SESSION_MAX = 8
UNLIMITED_TRIGGERS = {"pre_open_read", "close_summary", "engine_offline"}


class Watcher:
    def __init__(self, ctx: ToolContext):
        self._ctx = ctx
        self._redis: Redis = ctx.redis
        self._db: AsyncClient = ctx.db
        self._last_fired: dict[str, datetime] = {}
        self._session_count = 0
        self._suppressed = 0
        self._session_day: str | None = None
        self._did_preopen = False
        self._did_close = False
        self._engine_offline_posted = False

    # -- ping emission -------------------------------------------------------

    async def _muted(self) -> bool:
        raw = await self._redis.get(MUTE_KEY)
        if not raw:
            return False
        try:
            until = datetime.fromisoformat(raw if isinstance(raw, str) else raw.decode())
        except ValueError:
            return False
        return datetime.now(timezone.utc) < until

    def _rate_ok(self, trigger: str) -> bool:
        now = datetime.now(timezone.utc)
        if trigger in UNLIMITED_TRIGGERS:
            return True
        last = self._last_fired.get(trigger)
        if last and (now - last).total_seconds() < PER_TRIGGER_COOLDOWN_S:
            return False
        if self._session_count >= GLOBAL_SESSION_MAX:
            self._suppressed += 1
            return False
        return True

    async def ping(self, trigger: str, body: str) -> None:
        if await self._muted():
            return
        if not self._rate_ok(trigger):
            log.info("ping suppressed (%s)", trigger)
            return
        if self._suppressed:
            body += f"\n(+{self._suppressed} pings suppressed by the rate limit)"
            self._suppressed = 0
        self._last_fired[trigger] = datetime.now(timezone.utc)
        if trigger not in UNLIMITED_TRIGGERS:
            self._session_count += 1
        res = await self._db.table("pings").insert({"trigger": trigger, "body": body}).execute()
        row = res.data[0]
        await self._redis.publish(rk.PINGS_CHANNEL, json.dumps(row, default=str))
        log.info("ping [%s] %s", trigger, body.splitlines()[0][:120])

    # -- main loops ----------------------------------------------------------

    async def run_forever(self) -> None:
        await asyncio.gather(self._clock_loop(), self._event_loop())

    async def _clock_loop(self) -> None:
        while True:
            try:
                await self._on_clock()
            except Exception:  # noqa: BLE001
                log.exception("watcher clock tick failed")
            await asyncio.sleep(30)

    async def _event_loop(self) -> None:
        while True:
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(rk.EVENTS_CHANNEL)
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        data = msg["data"]
                        event = json.loads(data if isinstance(data, str) else data.decode())
                        await self._on_event(event)
                    except Exception:  # noqa: BLE001
                        log.exception("watcher event failed")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("watcher pubsub dropped; reconnecting in 5s")
                await asyncio.sleep(5)

    # -- clock triggers ------------------------------------------------------

    async def _on_clock(self) -> None:
        now_et = datetime.now(timezone.utc).astimezone(ET)
        day = now_et.date().isoformat()
        if day != self._session_day:
            self._session_day = day
            self._session_count = 0
            self._suppressed = 0
            self._did_preopen = False
            self._did_close = False
            self._last_fired.clear()

        if now_et.weekday() < 5:
            if not self._did_preopen and now_et.time() >= time(9, 20):
                self._did_preopen = True
                await self._pre_open_read(day)
            if not self._did_close and now_et.time() >= time(16, 30):
                self._did_close = True
                await self._close_summary(day)

        age = await rk.heartbeat_age_s(self._redis)
        if age is not None and age > rk.OFFLINE_AFTER_S:
            if not self._engine_offline_posted and now_et.weekday() < 5 and time(9, 0) <= now_et.time() <= time(16, 15):
                self._engine_offline_posted = True
                await self.ping("engine_offline", f"Engine offline — heartbeat {age:.0f}s old. Watcher paused until it returns.")
            return  # watcher pauses while offline
        self._engine_offline_posted = False

        await self._check_event_warning(now_et)
        await self._check_governor(day)

    async def _pre_open_read(self, day: str) -> None:
        result = await run_tool(self._ctx, "day_read", {})
        if not result.get("ok"):
            await self.ping("pre_open_read", f"09:20 day read unavailable: {result.get('reason')}")
            return
        data = result["data"]
        await self._db.table("sessions").update({"day_read_json": data}).eq("session_date", day).execute()
        await self._redis.set(LAST_DAY_READ_KEY, data.get("lean") or "no bias")
        lines = [f"09:20 day read: {data.get('lean', '?').upper()}"]
        if data.get("flip_condition"):
            lines.append(data["flip_condition"])
        if result.get("stale"):
            lines.insert(0, "⚠️ stale engine data")
        await self.ping("pre_open_read", "\n".join(lines))

    async def _close_summary(self, day: str) -> None:
        trades = await self._db.table("trades").select("net_pnl").eq("session_date", day).execute()
        pnls = [float(t["net_pnl"]) for t in trades.data or []]
        ops = await self._db.table("opinions").select("type", count="exact").gte("ts", f"{day}T00:00:00Z").execute()
        checklist = await self._db.table("checklist_entries").select("id", count="exact").eq("session_date", day).execute()
        await self.ping(
            "close_summary",
            f"Session close — {len(pnls)} trade(s), net {sum(pnls):+.2f}. "
            f"{checklist.count or 0} checklist entr{'y' if (checklist.count or 0) == 1 else 'ies'}, "
            f"{ops.count or 0} buddy opinion(s) logged (graded tonight). Journal the day while it's fresh.",
        )

    async def _check_event_warning(self, now_et: datetime) -> None:
        pos = await rk.read_json(self._redis, rk.POSITION)
        has_pos = bool(pos and pos.get("positions"))
        watches = await self._db.table("watches").select("id", count="exact").eq("active", True).execute()
        if not has_pos and not (watches.count or 0):
            return
        day = now_et.date().isoformat()
        events = (
            await self._db.table("calendar_events")
            .select("event_time_et, name, impact")
            .eq("event_date", day)
            .eq("impact", "high")
            .execute()
        )
        for ev in events.data or []:
            try:
                h, m = str(ev["event_time_et"]).split(":")[:2]
                ev_dt = now_et.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            except (ValueError, KeyError):
                continue
            delta_min = (ev_dt - now_et).total_seconds() / 60
            if 0 < delta_min <= 5:
                await self.ping("event_warning", f"{ev['name']} in {delta_min:.0f} min — high impact, you're exposed.")

    async def _check_governor(self, day: str) -> None:
        res = await self._db.table("checklist_entries").select("id", count="exact").eq("session_date", day).execute()
        taken = res.count or 0
        if taken == 1:
            await self.ping("governor", "Risk governor: 1 trade left today.")

    # -- engine-event triggers ----------------------------------------------

    async def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "bar_5m":
            await self._check_bias_flip()
            await self._check_stop_proximity()
        elif kind == "bar_1m":
            await self._check_watched_levels(event)
            await self._check_stop_proximity()

    async def _check_bias_flip(self) -> None:
        raw = await self._redis.get(LAST_DAY_READ_KEY)
        if raw is None:
            return  # nothing posted yet today
        last = raw if isinstance(raw, str) else raw.decode()
        result = await run_tool(self._ctx, "day_read", {})
        if not result.get("ok"):
            return
        lean = result["data"].get("lean") or "no bias"
        if lean != last:
            await self._redis.set(LAST_DAY_READ_KEY, lean)
            flip = result["data"].get("flip_condition") or ""
            await self.ping("bias_flip", f"Day read flipped: {last} → {lean.upper()}. {flip}")

    async def _check_stop_proximity(self) -> None:
        pos = await rk.read_json(self._redis, rk.POSITION)
        market = await rk.read_json(self._redis, rk.MARKET_STATE)
        if not pos or not market or not pos.get("positions") or market.get("last") is None:
            return
        last = float(market["last"])
        for p in pos["positions"]:
            stops = [
                o for o in pos.get("working_orders", [])
                if o.get("contract") == p.get("contract") and o.get("type") in ("Stop", "StopLimit") and o.get("price")
            ]
            for o in stops:
                dist = abs(last - float(o["price"]))
                if dist <= 5:
                    await self.ping(
                        "stop_proximity",
                        f"{p['contract']} {p['side']} {p['size']}: {dist:.2f} pts from your stop ({o['price']}). "
                        f"Decide now, not at the touch: honor it, or flatten first — never widen it.",
                    )
                    return

    async def _check_watched_levels(self, event: dict[str, Any]) -> None:
        q = event.get("impulse_q")
        close = event.get("close")
        if q is None or close is None or q < 90:
            return
        watches = await self._db.table("watches").select("id, price").eq("active", True).execute()
        for w in watches.data or []:
            if abs(float(w["price"]) - float(close)) <= 3:
                result = await run_tool(self._ctx, "level_read", {"price": float(w["price"])})
                if result.get("ok"):
                    d = result["data"]
                    await self.ping(
                        "flow_at_watch",
                        f"Flow Q{q:.0f} at your watched {w['price']}: lean {d.get('lean')} — {d.get('reason')}",
                    )
