"""DeskStateEngine — turns the tick/bar stream into the Redis contract.

Sync, cheap bookkeeping happens in tap() (called per tick, before bar
aggregation, same seam the shadow bot uses). Redis writes happen on closed
bars and on a 1s ticker. Flow metrics reuse nq_agent.flow verbatim so the
numbers match fc_t13; price bins and levels are desk-brain's own.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from nq_agent.flow import MinuteFlowAggregator, flow_over, minute_of_day_index
from nq_agent.models import Bar, Tick
from redis.asyncio import Redis

from .. import redis_keys as rk
from .levels import LevelBook
from .pricebins import PriceBinAggregator, value_area
from .qrank import QRank

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
RTH_FIRST_IDX = 9 * 60 + 31  # minute closing 09:31 ET
RTH_LAST_IDX = 16 * 60  # minute closing 16:00 ET

BAR_KEYS = {"1m": rk.BARS_1M, "5m": rk.BARS_5M, "15m": rk.BARS_15M}
BAR_CAPS = {"1m": 180, "5m": 72, "15m": 32}
BAR_SPANS = {"1m": 1, "5m": 5, "15m": 15}


class DeskStateEngine:
    def __init__(self, redis: Redis, qrank: QRank, level_book: LevelBook, regime_doc: dict[str, Any]):
        self._redis = redis
        self._qrank = qrank
        self._book = level_book
        self._regime = dict(regime_doc)

        self.flow = MinuteFlowAggregator(full_day=True)
        self.bins = PriceBinAggregator()

        self.last: float | None = None
        self.last_tick_at: datetime | None = None
        self.session_high: float | None = None
        self.session_low: float | None = None
        self.rth_open_price: float | None = None
        self._vwap_pv = 0.0
        self._vwap_v = 0.0
        self._session_profile: dict[float, float] = {}
        self._bars: dict[str, list[dict[str, Any]]] = {"1m": [], "5m": [], "15m": []}

    # -- hot path (sync, per tick) ------------------------------------------

    def tap(self, tick: Tick) -> None:
        self.flow.on_tick(tick)
        self.bins.on_tick(tick)
        price = float(tick.price)
        self.last = price
        self.last_tick_at = tick.ts
        et = tick.ts.astimezone(ET)
        if RTH_OPEN <= et.time() < RTH_CLOSE:
            if self.rth_open_price is None:
                self.rth_open_price = price
            self.session_high = price if self.session_high is None else max(self.session_high, price)
            self.session_low = price if self.session_low is None else min(self.session_low, price)
            self._vwap_pv += price * tick.size
            self._vwap_v += tick.size

    # -- bar-close path (async) ---------------------------------------------

    async def on_bar(self, bar: Bar) -> None:
        tf = bar.timeframe
        if tf not in BAR_KEYS:
            return
        close_et = bar.close_time.astimezone(ET)
        idx = minute_of_day_index(close_et)
        span = BAR_SPANS[tf]
        minutes = self.flow.minutes
        buy = sum(minutes.get(i, {}).get("buy", 0) for i in range(idx - span + 1, idx + 1))
        sell = sum(minutes.get(i, {}).get("sell", 0) for i in range(idx - span + 1, idx + 1))
        f1 = flow_over(minutes, idx, 5)
        doc = {
            "t": bar.close_time.astimezone(timezone.utc).isoformat(),
            "o": float(bar.open),
            "h": float(bar.high),
            "l": float(bar.low),
            "c": float(bar.close),
            "vol": bar.volume,
            "buy": buy,
            "sell": sell,
            "delta": buy - sell,
            "impulse": round(f1, 6),
            "impulse_q": self._qrank.rank(f1),
        }
        bars = self._bars[tf]
        bars.append(doc)
        del bars[: -BAR_CAPS[tf]]
        await rk.write_json(self._redis, BAR_KEYS[tf], {"bars": bars})

        if tf == "1m":
            await self._flush_bins()
            await self._write_market_state()
            await self._write_levels()
            await self._write_regime()
        if tf in ("1m", "5m"):
            await self._redis.publish(
                rk.EVENTS_CHANNEL,
                json.dumps({"kind": f"bar_{tf}", "t": doc["t"], "close": doc["c"],
                            "impulse": doc["impulse"], "impulse_q": doc["impulse_q"]}),
            )

    async def _flush_bins(self) -> None:
        now_min = int(datetime.now(timezone.utc).timestamp() // 60)
        done = self.bins.drain_before(now_min)
        for minute, bins in done.items():
            key = f"{rk.VBP_PREFIX}{minute}"
            await self._redis.hset(key, mapping=PriceBinAggregator.pack(bins))
            await self._redis.expire(key, rk.VBP_TTL_S)
            # accumulate the developing (RTH) profile
            minute_et = datetime.fromtimestamp(minute * 60, tz=timezone.utc).astimezone(ET)
            if RTH_OPEN <= minute_et.time() < RTH_CLOSE:
                for price, (b, s, _) in bins.items():
                    self._session_profile[price] = self._session_profile.get(price, 0.0) + b + s

    # -- periodic (1s ticker calls this too, for last/heartbeat freshness) ---

    async def heartbeat_if_alive(self) -> None:
        if self.last_tick_at is None:
            return
        age = (datetime.now(timezone.utc) - self.last_tick_at.astimezone(timezone.utc)).total_seconds()
        if age <= 20:
            await rk.beat(self._redis)

    async def _write_market_state(self) -> None:
        now_et = datetime.now(timezone.utc).astimezone(ET)
        minutes_in = 0
        if now_et.time() >= RTH_OPEN:
            minutes_in = min(390, int((now_et - now_et.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() // 60))
        cum_delta = sum(
            m.get("buy", 0) - m.get("sell", 0)
            for i, m in self.flow.minutes.items()
            if RTH_FIRST_IDX <= i <= RTH_LAST_IDX
        )
        prior_close = self._book.prior_close
        gap = None
        ref = self.rth_open_price if self.rth_open_price is not None else self.last
        if prior_close is not None and ref is not None:
            gap = round(ref - prior_close, 2)
        await rk.write_json(
            self._redis,
            rk.MARKET_STATE,
            {
                "last": self.last,
                "session_high": self.session_high,
                "session_low": self.session_low,
                "on_high": self._book.on_high,
                "on_low": self._book.on_low,
                "prior_close": prior_close,
                "gap_vs_prior_close": gap,
                "vwap": round(self._vwap_pv / self._vwap_v, 2) if self._vwap_v > 0 else None,
                "prior_value_area": self._book.prior_value_area,
                "developing_value_area": value_area(self._session_profile),
                "cum_delta_rth": cum_delta,
                "minutes_into_session": minutes_in,
            },
        )

    async def write_market_state_now(self) -> None:
        await self._write_market_state()

    async def _write_levels(self) -> None:
        await rk.write_json(
            self._redis, rk.LEVELS, self._book.snapshot(self.last, self.session_high, self.session_low)
        )

    async def _write_regime(self) -> None:
        closes = [b["c"] for b in self._bars["1m"][-31:]]
        rv30 = None
        if len(closes) >= 10:
            rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
            if rets:
                mean = sum(rets) / len(rets)
                var = sum((x - mean) ** 2 for x in rets) / len(rets)
                rv30 = round(math.sqrt(var) * math.sqrt(390 * 252) * 100, 2)  # annualized, %
        doc = dict(self._regime)
        doc["rv30_annualized_pct"] = rv30
        await rk.write_json(self._redis, rk.REGIME, doc)
