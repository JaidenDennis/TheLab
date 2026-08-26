"""State runner: bootstrap (levels, gamma, qrank) then stream ticks/bars.

Anchors the process at the repo root before touching nq_agent config paths
(config/base.yaml and .env are read relative to the CWD — known gotcha).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from nq_agent.feed.databento import DatabentoFeed
from nq_agent.feed.reconnecting import ReconnectingFeed
from redis.asyncio import Redis

from .. import redis_keys as rk
from ..config import Settings
from ..tools.signals import load_params
from ..tools.signals import vol as vol_sig
from .engine import DeskStateEngine
from .gamma import flow_regime_today, gamma_walls, latest_regime_row, trailing_percentile
from .levels import LevelBook
from .qrank import QRank

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
SYMBOL = "NQ.v.0"  # volume-rolled continuous, matches shadow.yaml and the fixtures
DATASET = "GLBX.MDP3"
HIST_DAYS = 12


async def build_engine(s: Settings, redis: Redis) -> tuple[DeskStateEngine, str]:
    os.chdir(s.repo_root)

    api_key = s.databento_api_key or os.environ.get("NQ_DATABENTO_API_KEY", "")
    if not api_key:
        raise SystemExit("DATABENTO_API_KEY / NQ_DATABENTO_API_KEY not set")

    qrank = QRank.from_flow_store(s.repo_root / "var" / "flow")

    hist_feed = DatabentoFeed(api_key=api_key, symbol=SYMBOL, dataset=DATASET)
    now = datetime.now(timezone.utc)
    session_date = now.astimezone(ET).date()
    # GLBX historical availability lags real time by a few minutes; end=now 422s
    # (data_end_after_available_end). The live stream resumes from now-5m anyway.
    hist_end = now - timedelta(minutes=15)
    hist = await hist_feed.get_bars(SYMBOL, "1m", now - timedelta(days=HIST_DAYS), hist_end)
    log.info("state: %d historical 1m bars for level bootstrap", len(hist))
    book = LevelBook.build(hist, session_date)

    gamma_dir = s.repo_root / "var" / "gamma"
    regime_doc: dict[str, Any] = {"gamma_sign": None, "gamma_magnitude": None, "flow_regime": None}
    row = latest_regime_row(gamma_dir)
    if row:
        regime_doc["gamma_sign"] = row.get("regime")
        regime_doc["gamma_magnitude"] = {
            "net_gex": row.get("net_gex"),
            "trailing_pctile": trailing_percentile(gamma_dir, row.get("net_gex") or 0.0),
            "as_of_date": row.get("date"),
            "stale_snapshot": row.get("date") != session_date.isoformat(),
        }
        spot = row.get("spot")
        if spot and row.get("date"):
            walls = gamma_walls(gamma_dir, row["date"], float(spot))
            if walls:
                # QQQ strikes are not NQ prices — report them, don't mix into NQ levels.
                regime_doc["gamma_walls_qqq"] = walls
    regime_doc["flow_regime"] = flow_regime_today(s.repo_root / "var" / "decisions" / "k3m", session_date.isoformat())

    params = load_params(s.signals_path)
    await rk.write_json(redis, rk.DAILY, _daily_doc(hist, session_date, params))

    engine = DeskStateEngine(redis, qrank, book, regime_doc, signal_params=params)
    return engine, api_key


def _daily_doc(hist_1m: list, session_date, params: dict) -> dict[str, Any]:
    """Daily aggregates for the signal layer: OHLC rows, ATR, IB and ON range
    history — computed once at bootstrap from the same bars the levels use."""
    from collections import defaultdict
    from datetime import time as dtime

    by_day: dict[Any, list] = defaultdict(list)
    for b in hist_1m:
        by_day[b.open_time.astimezone(ET).date()].append(b)
    daily_rows: list[dict[str, Any]] = []
    ib_ranges: list[float] = []
    on_ranges: list[float] = []
    days = sorted(d for d in by_day if d < session_date)
    for i, d in enumerate(days):
        rth = [b for b in by_day[d] if dtime(9, 30) <= b.open_time.astimezone(ET).time() < dtime(16, 0)]
        if not rth:
            continue
        daily_rows.append({
            "date": d.isoformat(),
            "o": float(rth[0].open), "h": float(max(b.high for b in rth)),
            "l": float(min(b.low for b in rth)), "c": float(rth[-1].close),
        })
        ib = [b for b in rth if b.open_time.astimezone(ET).time() < dtime(10, 30)]
        if ib:
            ib_ranges.append(float(max(b.high for b in ib) - min(b.low for b in ib)))
        prev = days[i - 1] if i > 0 else None
        if prev is not None:
            on = [
                b for src in (prev, d) for b in by_day[src]
                if (b.open_time.astimezone(ET).date() == prev and b.open_time.astimezone(ET).time() >= dtime(18, 0))
                or (b.open_time.astimezone(ET).date() == d and b.open_time.astimezone(ET).time() < dtime(9, 30))
            ]
            if on:
                on_ranges.append(float(max(b.high for b in on) - min(b.low for b in on)))
    return {
        "daily": daily_rows,
        "atr": vol_sig.atr(daily_rows, params["vol"]["atr_days"]),
        "ib_ranges": ib_ranges,
        "on_ranges": on_ranges,
    }


async def run_state(s: Settings, redis: Redis) -> None:
    engine, api_key = await build_engine(s, redis)
    live = DatabentoFeed(api_key=api_key, symbol=SYMBOL, dataset=DATASET, tick_tap=engine.tap)
    feed = ReconnectingFeed(live, max_attempts=1_000_000, initial_backoff=1.0, max_backoff=60.0)

    async def ticker() -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await engine.heartbeat_if_alive()
                if engine.last is not None:
                    await engine.write_market_state_now()
                    await engine.write_tape_now()
            except Exception:  # noqa: BLE001
                log.exception("state ticker failed")

    tick_task = asyncio.create_task(ticker())
    try:
        resume = datetime.now(timezone.utc) - timedelta(minutes=5)
        async for bar in feed.stream(SYMBOL, ["1m", "5m", "15m"], resume_from=resume):
            await engine.on_bar(bar)
    finally:
        tick_task.cancel()
