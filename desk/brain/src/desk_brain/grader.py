"""Nightly opinion grader + Friday scorecard ping (spec §16).

Grading is deterministic against Databento 1m history — never against the
model's memory of the day. Horizons:
- level:  MFE/MAE race over 15 min; hit if 10 pts favorable before 10 pts adverse.
- day:    sign of RTH close minus price at call (flip-level breach noted if stated).
- manage: suggested exit vs holding over the linked trade's remaining life.
Brier score on stated confidence where present.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from nq_agent.feed.databento import DatabentoFeed
from supabase import AsyncClient

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
RACE_PTS = 10.0
HORIZON_MIN = 15


class Grader:
    def __init__(self, db: AsyncClient, databento_api_key: str, symbol: str = "NQ.v.0"):
        self._db = db
        self._feed = DatabentoFeed(api_key=databento_api_key, symbol=symbol)
        self._symbol = symbol

    async def run_forever(self) -> None:
        while True:
            now_et = datetime.now(timezone.utc).astimezone(ET)
            target = now_et.replace(hour=17, minute=5, second=0, microsecond=0)
            if now_et >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now_et).total_seconds())
            try:
                graded = await self.grade_pending()
                log.info("grader: %d opinion(s) graded", graded)
                if datetime.now(timezone.utc).astimezone(ET).weekday() == 4:  # Friday
                    await self.weekly_ping()
            except Exception:  # noqa: BLE001
                log.exception("grader run failed")

    async def grade_pending(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=HORIZON_MIN + 5)
        res = (
            await self._db.table("opinions")
            .select("*")
            .is_("graded_at", None)
            .lt("ts", cutoff.isoformat())
            .order("ts")
            .limit(200)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return 0

        graded = 0
        for op in rows:
            try:
                outcome, score = await self.grade_one(op)
            except Exception:  # noqa: BLE001
                log.exception("grading opinion %s failed", op["id"])
                continue
            await (
                self._db.table("opinions")
                .update({"graded_at": datetime.now(timezone.utc).isoformat(), "outcome": outcome, "score": score})
                .eq("id", op["id"])
                .execute()
            )
            graded += 1
        return graded

    async def _bars_after(self, ts: datetime, minutes: int) -> list[Any]:
        start = ts.replace(second=0, microsecond=0)
        return await self._feed.get_bars(self._symbol, "1m", start, start + timedelta(minutes=minutes + 1))

    async def grade_one(self, op: dict[str, Any]) -> tuple[str, float | None]:
        direction = (op.get("factors_json") or {}).get("direction")
        ts = datetime.fromisoformat(op["ts"].replace("Z", "+00:00"))
        price = float(op["price"]) if op.get("price") is not None else None
        conf = op.get("confidence")

        if direction in (None, "flat") and op["type"] != "day":
            return "ungradeable: no direction recorded", None
        if price is None:
            return "ungradeable: no price recorded", None

        if op["type"] == "level":
            bars = await self._bars_after(ts, HORIZON_MIN)
            if not bars:
                return "ungradeable: no bars", None
            sign = 1 if direction == "long" else -1 if direction == "short" else 0
            if sign == 0:
                # no-go graded inverted: a no-go "hits" if neither side would have paid
                hit_long = _race(bars, price, 1)
                hit_short = _race(bars, price, -1)
                hit = not (hit_long or hit_short)
            else:
                hit = _race(bars, price, sign)
            score = _brier(conf, hit)
            return f"{'hit' if hit else 'miss'} (10pt race, 15m)", score

        if op["type"] == "day":
            close_dt = ts.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            if close_dt <= ts:
                return "ungradeable: called after close", None
            bars = await self._feed.get_bars(self._symbol, "1m", close_dt - timedelta(minutes=3), close_dt + timedelta(minutes=1))
            if not bars:
                return "ungradeable: no close bar", None
            close_px = float(bars[-1].close)
            move = close_px - price
            if direction == "long":
                hit = move > 0
            elif direction == "short":
                hit = move < 0
            else:  # "no bias" graded as: close within ±0.25% of call price
                hit = abs(move) / price <= 0.0025
            return f"{'hit' if hit else 'miss'} (close {move:+.2f} vs call)", _brier(conf, hit)

        if op["type"] == "manage":
            if not op.get("trade_id"):
                return "ungradeable: no linked trade", None
            trade = (
                await self._db.table("trades").select("exit_at, avg_exit, direction").eq("id", op["trade_id"]).maybe_single().execute()
            )
            t = getattr(trade, "data", None)
            if not t:
                return "ungradeable: linked trade missing", None
            exit_px = float(t["avg_exit"])
            pos_sign = 1 if t["direction"] == "long" else -1
            hold_pnl = (exit_px - price) * pos_sign
            if direction in ("flat", "no-go"):  # suggested exit at opinion price
                hit = hold_pnl < 0  # exiting beat holding iff holding lost from there
            else:
                hit = hold_pnl > 0  # suggested staying with the position
            return f"{'hit' if hit else 'miss'} (hold pnl from call: {hold_pnl:+.2f} pts)", _brier(conf, hit)

        return "ungradeable: unknown type", None

    async def weekly_ping(self) -> None:
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        res = await self._db.table("opinions").select("type, outcome, score, factors_json").gte("ts", week_ago).execute()
        rows = [r for r in (res.data or []) if r.get("outcome")]
        graded = [r for r in rows if r["outcome"].startswith(("hit", "miss"))]
        by_type: dict[str, list[bool]] = {}
        val_only: list[bool] = []
        mixed: list[bool] = []
        briers = [r["score"] for r in graded if r.get("score") is not None]
        for r in graded:
            hit = r["outcome"].startswith("hit")
            by_type.setdefault(r["type"], []).append(hit)
            if (r.get("factors_json") or {}).get("validated_cited"):
                val_only.append(hit)
            else:
                mixed.append(hit)
        lines = [f"Weekly buddy scorecard — {len(graded)} graded opinion(s) of {len(res.data or [])}."]
        for typ, hits in sorted(by_type.items()):
            lines.append(f"  {typ}: {sum(hits)}/{len(hits)} hit")
        if briers:
            lines.append(f"  Brier (stated confidence): {sum(briers)/len(briers):.3f} over {len(briers)}")
        lines.append(f"  cited-validated {sum(val_only)}/{len(val_only)} vs no-validated-cited {sum(mixed)}/{len(mixed)}")
        if len(val_only) + len(mixed) >= 40 and val_only and mixed:
            if (sum(mixed) / len(mixed)) < (sum(val_only) / len(val_only)):
                lines.append("  ≥40 samples: opinions citing no validated factor are underperforming — weight the tags.")
        body = "\n".join(lines)
        row = await self._db.table("pings").insert({"trigger": "weekly_scorecard", "body": body}).execute()
        log.info("weekly scorecard posted: %s", row.data[0]["id"])


def _race(bars: list[Any], price: float, sign: int) -> bool:
    """True if 10 pts favorable is reached before 10 pts adverse within the bars.
    Adverse-first within-bar rule where 1m OHLC is ambiguous (house convention)."""
    for b in bars:
        hi, lo = float(b.high), float(b.low)
        fav = (hi - price) if sign > 0 else (price - lo)
        adv = (price - lo) if sign > 0 else (hi - price)
        if adv >= RACE_PTS:
            return False
        if fav >= RACE_PTS:
            return True
    return False


def _brier(conf: float | None, hit: bool) -> float | None:
    if conf is None:
        return None
    return round((float(conf) - (1.0 if hit else 0.0)) ** 2, 4)
