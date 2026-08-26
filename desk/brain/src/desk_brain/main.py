"""desk-brain composition root: one process, asyncio tasks (spec §3).

Tasks: state engine (Databento -> Redis), Tradovate user sync + fill pump +
position writer, watcher, grader, and the internal HTTP/SSE API. Any task
dying is logged and restarted with backoff — a silent half-dead brain during
RTH is the failure mode this file exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
import uvicorn
from redis.asyncio import Redis

from .api import build_app
from .agent.loop import BuddyAgent
from .agent.memory import Memory
from .config import settings
from .db import make_db
from .factors import load_factors
from .fill_pump import FillPump
from .grader import Grader
from .position_writer import PositionWriter
from .state.runner import run_state
from .tools import ToolContext
from .tools.signals import load_params
from .tradovate import TradovateREST, UserSync
from .watcher import Watcher

log = logging.getLogger(__name__)


async def supervised(name: str, coro_factory) -> None:
    backoff = 2.0
    while True:
        try:
            await coro_factory()
            log.warning("task %s exited cleanly; restarting in %.0fs", name, backoff)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("task %s crashed; restarting in %.0fs", name, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120.0)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = settings()
    os.chdir(s.repo_root)

    redis = Redis.from_url(s.redis_url, decode_responses=True)
    db = await make_db()
    factors = load_factors(s.factors_path)
    params = load_params(s.signals_path)
    ctx = ToolContext(redis=redis, db=db, settings=s, factors=factors, params=params)
    memory = Memory(db)
    agent = BuddyAgent(s, ctx, memory, factors)
    watcher = Watcher(ctx)

    tasks: list[asyncio.Task] = []

    # Market state engine (Databento). Optional in dev without a key.
    if s.databento_api_key or os.environ.get("NQ_DATABENTO_API_KEY"):
        tasks.append(asyncio.create_task(supervised("state", lambda: run_state(s, redis))))
    else:
        log.warning("no Databento key — state engine disabled; tools will report offline")

    # Tradovate (position + fill auto-pull). Optional in dev without creds.
    if s.tradovate_username and s.tradovate_cid:
        http = aiohttp.ClientSession()
        rest = TradovateREST(s, http)
        sync = UserSync(s, rest)
        writer = PositionWriter(sync, redis)
        web_url = os.environ.get("WEB_URL", "http://localhost:3000")
        FillPump(s, sync, db, http, web_url)
        tasks.append(asyncio.create_task(supervised("tradovate", sync.run_forever)))
        tasks.append(asyncio.create_task(supervised("position", writer.run_forever)))
    else:
        log.warning("no Tradovate credentials — position/fill auto-pull disabled")

    tasks.append(asyncio.create_task(supervised("watcher", watcher.run_forever)))

    databento_key = s.databento_api_key or os.environ.get("NQ_DATABENTO_API_KEY", "")
    if databento_key:
        grader = Grader(db, databento_key)
        tasks.append(asyncio.create_task(supervised("grader", grader.run_forever)))

    app = build_app(agent, memory, ctx)
    config = uvicorn.Config(app, host=s.brain_host, port=s.brain_port, log_level="info")
    server = uvicorn.Server(config)
    log.info("desk-brain up on %s:%d", s.brain_host, s.brain_port)
    try:
        await server.serve()
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
