"""Tool envelope: ok/stale/failure semantics against fakeredis (spec §21)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis.aioredis
import pytest

from desk_brain import redis_keys as rk
from desk_brain.config import Settings
from desk_brain.factors import load_factors
from desk_brain.tools import ToolContext, run_tool

FACTORS = load_factors(Path(__file__).resolve().parents[2] / "factors.yaml")


@pytest.fixture
def ctx():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return ToolContext(redis=redis, db=None, settings=Settings(), factors=FACTORS)


async def seed(redis, heartbeat_age_s: float = 0.0):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_s)).isoformat()
    await redis.set(rk.HEARTBEAT, json.dumps({"ts": ts}))
    await redis.set(rk.MARKET_STATE, json.dumps({"last": 20150.0, "ts": ts}))


async def test_fresh_heartbeat_not_stale(ctx):
    await seed(ctx.redis, 1.0)
    out = await run_tool(ctx, "market_state", {})
    assert out["ok"] is True
    assert out["stale"] is False
    assert out["data"]["last"] == 20150.0
    assert "as_of" in out


async def test_old_heartbeat_flags_stale(ctx):
    await seed(ctx.redis, 12.0)
    out = await run_tool(ctx, "market_state", {})
    assert out["ok"] is True
    assert out["stale"] is True


async def test_missing_state_is_failure_not_guess(ctx):
    out = await run_tool(ctx, "levels", {})
    assert out["ok"] is False
    assert "engine has not written" in out["reason"]


async def test_unknown_tool(ctx):
    out = await run_tool(ctx, "place_order", {})
    assert out["ok"] is False
    assert "unknown tool" in out["reason"]


async def test_footprint_aggregates_band(ctx):
    await seed(ctx.redis, 1.0)
    now_min = int(datetime.now(timezone.utc).timestamp() // 60)
    await ctx.redis.hset(f"{rk.VBP_PREFIX}{now_min}", mapping={"20100.0": "30,10,12", "20100.25": "5,25,8", "20200.0": "99,99,99"})
    out = await run_tool(ctx, "footprint_at_level", {"low": 20099, "high": 20101, "lookback_min": 5})
    assert out["ok"] is True
    d = out["data"]
    assert d["volume"] == 70
    assert d["delta"] == 0
    assert d["absorption_score"] == 1.0
    assert d["largest_prints"][0]["price"] == 20100.0
