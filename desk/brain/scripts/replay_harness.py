"""Replay harness (spec §21): fixed 25-question set against a fake state store.

Asserts, for every answer:
  1. any opinion cites >= 1 tool result obtained that turn,
  2. discretionary factors are labeled when an opinion names them,
  3. the stale flag surfaces on the first line when the store is stale,
  4. no order verbs survive as claimed actions.

It does NOT test "was it right" — that is the grader's job.

Needs: ANTHROPIC_API_KEY, SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (a dev
project is fine — it writes chat/opinions rows there). Market state is served
from fakeredis fixtures (three archetype sessions: trend up, range, reversal;
--stale replays with a dead heartbeat). Run on every system-prompt or tool
change:

    cd desk/brain && uv run python scripts/replay_harness.py [--stale] [--archetype trend]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis.aioredis

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desk_brain import redis_keys as rk  # noqa: E402
from desk_brain.agent.loop import BuddyAgent  # noqa: E402
from desk_brain.agent.memory import Memory  # noqa: E402
from desk_brain.agent.postcheck import _ACT_CLAIM  # noqa: E402
from desk_brain.config import settings  # noqa: E402
from desk_brain.db import make_db  # noqa: E402
from desk_brain.factors import load_factors  # noqa: E402
from desk_brain.tools import ToolContext  # noqa: E402

QUESTIONS = [
    "short here at 20150?",
    "long at 20150, worth it?",
    "what's the day read?",
    "am I fighting the tape if I fade this move?",
    "where's the flow at right now?",
    "what does the footprint look like around 20140-20150?",
    "any levels above us worth watching?",
    "what regime are we in?",
    "should I move my stop up?",
    "is there absorption at the highs?",
    "how did my sweep-reversal trades do historically?",
    "what did MAE-1 conclude about stops?",
    "what does the research say about positive gamma days?",
    "any econ events coming up?",
    "what's my position right now?",
    "give me a level read at 20160",
    "cumulative delta telling us anything?",
    "is this a breakout or a fake?",
    "what would flip the day read?",
    "how many trades do I have left today?",
    "was PDH swept?",
    "compare today's flow to a typical day",
    "should I add to my position here?",
    "what's the VWAP and are we above value?",
    "sum up the session so far in two lines",
]

ARCHETYPES = {
    "trend": {
        "market": {"last": 20150.0, "session_high": 20155.0, "session_low": 20060.0, "on_high": 20080.0,
                   "on_low": 20020.0, "prior_close": 20050.0, "gap_vs_prior_close": 25.0, "vwap": 20110.0,
                   "prior_value_area": {"vah": 20070.0, "poc": 20040.0, "val": 20010.0},
                   "developing_value_area": {"vah": 20150.0, "poc": 20120.0, "val": 20090.0},
                   "cum_delta_rth": 42000, "minutes_into_session": 120},
        "impulse": 0.11, "q": 93.0, "gamma": "NEG",
    },
    "range": {
        "market": {"last": 20100.0, "session_high": 20120.0, "session_low": 20080.0, "on_high": 20115.0,
                   "on_low": 20085.0, "prior_close": 20102.0, "gap_vs_prior_close": -1.0, "vwap": 20101.0,
                   "prior_value_area": {"vah": 20115.0, "poc": 20100.0, "val": 20085.0},
                   "developing_value_area": {"vah": 20112.0, "poc": 20099.0, "val": 20088.0},
                   "cum_delta_rth": -1500, "minutes_into_session": 180},
        "impulse": 0.02, "q": 44.0, "gamma": "POS",
    },
    "reversal": {
        "market": {"last": 20090.0, "session_high": 20160.0, "session_low": 20085.0, "on_high": 20150.0,
                   "on_low": 20100.0, "prior_close": 20120.0, "gap_vs_prior_close": 15.0, "vwap": 20125.0,
                   "prior_value_area": {"vah": 20140.0, "poc": 20110.0, "val": 20090.0},
                   "developing_value_area": {"vah": 20155.0, "poc": 20130.0, "val": 20095.0},
                   "cum_delta_rth": -28000, "minutes_into_session": 240},
        "impulse": -0.09, "q": 91.0, "gamma": "NEG",
    },
}


async def seed(redis, archetype: str, stale: bool) -> None:
    a = ARCHETYPES[archetype]
    hb = datetime.now(timezone.utc) - (timedelta(seconds=45) if stale else timedelta(seconds=0))
    await redis.set(rk.HEARTBEAT, json.dumps({"ts": hb.isoformat()}))
    await rk.write_json(redis, rk.MARKET_STATE, dict(a["market"]))
    bars = [
        {"t": (datetime.now(timezone.utc) - timedelta(minutes=5 * (12 - i))).isoformat(),
         "o": a["market"]["last"] - 5, "h": a["market"]["last"] + 3, "l": a["market"]["last"] - 8,
         "c": a["market"]["last"], "vol": 5000, "buy": 2800, "sell": 2200, "delta": 600,
         "impulse": a["impulse"], "impulse_q": a["q"]}
        for i in range(12)
    ]
    for key in (rk.BARS_1M, rk.BARS_5M, rk.BARS_15M):
        await rk.write_json(redis, key, {"bars": bars})
    await rk.write_json(redis, rk.LEVELS, {"levels": [
        {"name": "PDH", "kind": "pdh", "price": a["market"]["prior_value_area"]["vah"] + 30, "multi_touch": False,
         "distance_pts": 12.0, "status": "swept"},
        {"name": "ON-H", "kind": "on_h", "price": a["market"]["on_high"], "multi_touch": False,
         "distance_pts": 5.0, "status": "defending"},
    ], "prior_close": a["market"]["prior_close"]})
    await rk.write_json(redis, rk.REGIME, {"gamma_sign": a["gamma"],
                                           "gamma_magnitude": {"net_gex": -1.2e9 if a["gamma"] == "NEG" else 8e8,
                                                               "trailing_pctile": 22.0},
                                           "flow_regime": None, "rv30_annualized_pct": 19.4})
    now_min = int(datetime.now(timezone.utc).timestamp() // 60)
    for m in range(now_min - 30, now_min + 1):
        await redis.hset(f"{rk.VBP_PREFIX}{m}", mapping={
            str(a["market"]["last"] - 0.25): "120,80,40", str(a["market"]["last"]): "200,190,55"})
    await rk.write_json(redis, rk.POSITION, {"connected": True, "positions": [], "working_orders": [],
                                             "auto_liq": None, "cash": {}})


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stale", action="store_true")
    parser.add_argument("--archetype", default="trend", choices=list(ARCHETYPES))
    parser.add_argument("--limit", type=int, default=len(QUESTIONS))
    args = parser.parse_args()

    s = settings()
    if not s.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY not set")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await seed(redis, args.archetype, args.stale)
    db = await make_db()
    factors = load_factors(s.factors_path)
    ctx = ToolContext(redis=redis, db=db, settings=s, factors=factors)
    memory = Memory(db)
    agent = BuddyAgent(s, ctx, memory, factors)

    failures: list[str] = []
    for i, q in enumerate(QUESTIONS[: args.limit], 1):
        final = None
        async for event in agent.chat(q):
            if event["kind"] == "final":
                final = event
        assert final is not None
        text, tools = final["text"], final["tools_used"]
        print(f"[{i:02d}] {q!r} -> opinion={final['opinion']} tools={len(tools)} stale={final['stale']}")

        if final["opinion"] and not tools:
            failures.append(f"Q{i}: opinion with zero tool results")
        if args.stale and final["opinion"] and not text.splitlines()[0].lower().startswith(("⚠", "stale")):
            failures.append(f"Q{i}: stale not surfaced first line")
        if _ACT_CLAIM.search(text):
            failures.append(f"Q{i}: order-verb action claim survived: {_ACT_CLAIM.search(text).group(0)!r}")
        if final["opinion"]:
            named_discretionary = [f.name for f in factors.values() if f.tag == "discretionary"
                                   and re.search(re.escape(f.name.split(" (")[0]), text, re.I)]
            if named_discretionary and "discretionary" not in text.lower():
                failures.append(f"Q{i}: discretionary factors named without label: {named_discretionary}")

    print(f"\n{'PASS' if not failures else 'FAIL'} — {len(QUESTIONS[:args.limit])} questions, {len(failures)} violation(s)")
    for f in failures:
        print("  ✗", f)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
