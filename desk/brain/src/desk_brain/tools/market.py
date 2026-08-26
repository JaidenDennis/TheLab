"""Market-state tools: pure reads of the Redis contract (spec §13)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .. import redis_keys as rk
from . import ToolContext, tool


@tool(
    "market_state",
    {
        "description": (
            "Current tape snapshot: last price, session and overnight high/low, gap vs prior "
            "close, VWAP, value areas (prior day and developing), cumulative delta since RTH "
            "open, minutes into session."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)
async def market_state(ctx: ToolContext, args: dict[str, Any]) -> Any:
    doc = await rk.read_json(ctx.redis, rk.MARKET_STATE)
    if doc is None:
        raise RuntimeError("no market state in store — engine has not written yet")
    return doc


_BAR_KEYS = {"1m": rk.BARS_1M, "5m": rk.BARS_5M, "15m": rk.BARS_15M}


@tool(
    "flow",
    {
        "description": (
            "Per-bar aggressor flow for a timeframe: buy/sell aggressor volume, delta, flow "
            "impulse (f1_5: 5-minute aggressor delta ratio, the validated TFR metric), and the "
            "impulse Q-rank vs the fc_t13 reference distribution (Q90+ = confirm threshold)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tf": {"type": "string", "enum": ["1m", "5m", "15m"], "description": "bar timeframe"},
                "n": {"type": "integer", "minimum": 1, "maximum": 60, "description": "how many recent bars"},
            },
            "required": ["tf"],
            "additionalProperties": False,
        },
    },
)
async def flow(ctx: ToolContext, args: dict[str, Any]) -> Any:
    tf = args.get("tf", "5m")
    n = int(args.get("n", 12))
    key = _BAR_KEYS.get(tf)
    if key is None:
        raise ValueError(f"unsupported timeframe {tf!r}")
    doc = await rk.read_json(ctx.redis, key)
    if doc is None:
        raise RuntimeError(f"no {tf} bars in store yet")
    bars = doc.get("bars", [])[-n:]
    return {"tf": tf, "bars": bars, "note": "impulse=f1_5 trailing 5m; impulse_q is empirical percentile of |f1_5|"}


@tool(
    "footprint_at_level",
    {
        "description": (
            "Aggressor volume by price inside a band over a lookback window: delta, total "
            "volume, absorption score (aggression that failed to move price), stacked "
            "imbalances, and the largest single-minute prints in the band."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "low": {"type": "number", "description": "band low price"},
                "high": {"type": "number", "description": "band high price"},
                "lookback_min": {"type": "integer", "minimum": 1, "maximum": 120, "description": "minutes back (default 30)"},
            },
            "required": ["low", "high"],
            "additionalProperties": False,
        },
    },
)
async def footprint_at_level(ctx: ToolContext, args: dict[str, Any]) -> Any:
    low, high = float(args["low"]), float(args["high"])
    if high < low:
        low, high = high, low
    lookback = int(args.get("lookback_min", 30))

    now_min = int(datetime.now(timezone.utc).timestamp() // 60)
    per_price: dict[float, dict[str, float]] = {}
    minutes_seen = 0
    for m in range(now_min - lookback + 1, now_min + 1):
        h = await ctx.redis.hgetall(f"{rk.VBP_PREFIX}{m}")
        if not h:
            continue
        minutes_seen += 1
        for price_raw, packed in h.items():
            price = float(price_raw if isinstance(price_raw, str) else price_raw.decode())
            if not (low <= price <= high):
                continue
            if isinstance(packed, bytes):
                packed = packed.decode()
            b, s, mx = (float(x) for x in packed.split(","))
            cell = per_price.setdefault(price, {"buy": 0.0, "sell": 0.0, "max_print": 0.0})
            cell["buy"] += b
            cell["sell"] += s
            cell["max_print"] = max(cell["max_print"], mx)

    if not per_price:
        return {
            "low": low, "high": high, "lookback_min": lookback,
            "volume": 0, "delta": 0, "note": "no traded volume in band over lookback",
        }

    volume = sum(c["buy"] + c["sell"] for c in per_price.values())
    delta = sum(c["buy"] - c["sell"] for c in per_price.values())
    # Absorption: heavy two-sided volume with small net delta reads as absorbed
    # aggression. 0 = fully directional, 1 = fully absorbed. Discretionary framing.
    absorption = 1.0 - (abs(delta) / volume) if volume > 0 else 0.0

    ladder = sorted(per_price.items())
    imbalance_stacks: list[dict[str, Any]] = []
    run: list[float] = []
    run_side: str | None = None
    for price, c in ladder:
        buy, sell = c["buy"], c["sell"]
        side = "buy" if buy >= 3 * max(sell, 1) else "sell" if sell >= 3 * max(buy, 1) else None
        if side and side == run_side:
            run.append(price)
        else:
            if run_side and len(run) >= 3:
                imbalance_stacks.append({"side": run_side, "prices": run})
            run = [price] if side else []
            run_side = side
    if run_side and len(run) >= 3:
        imbalance_stacks.append({"side": run_side, "prices": run})

    largest = sorted(
        ({"price": p, "max_single_minute_volume": c["max_print"]} for p, c in per_price.items()),
        key=lambda x: -x["max_single_minute_volume"],
    )[:5]

    return {
        "low": low,
        "high": high,
        "lookback_min": lookback,
        "minutes_with_data": minutes_seen,
        "volume": round(volume),
        "delta": round(delta),
        "absorption_score": round(absorption, 3),
        "imbalance_stacks": imbalance_stacks,
        "largest_prints": largest,
    }


async def read_vbp_minutes(ctx: ToolContext, first_min: int, last_min: int) -> list[dict[float, dict[str, float]]]:
    """Per-minute footprint cells from the engine's VBP hashes, oldest first.
    Shared by the footprint tool and the composite reads."""
    out: list[dict[float, dict[str, float]]] = []
    for m in range(first_min, last_min + 1):
        h = await ctx.redis.hgetall(f"{rk.VBP_PREFIX}{m}")
        cells: dict[float, dict[str, float]] = {}
        for price_raw, packed in (h or {}).items():
            price = float(price_raw if isinstance(price_raw, str) else price_raw.decode())
            if isinstance(packed, bytes):
                packed = packed.decode()
            b, s, mx = (float(x) for x in packed.split(","))
            cells[price] = {"buy": b, "sell": s, "max_print": mx}
        out.append(cells)
    return out


@tool(
    "tape",
    {
        "description": (
            "Live tape read from the last few minutes of raw prints: tape speed (and "
            "whether it's a session-percentile spike), delta rate over 10/30/60s, large "
            "prints and same-side clusters, print-size shift vs session median, and "
            "absorption at the current price if any. Refreshed every second."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)
async def tape(ctx: ToolContext, args: dict[str, Any]) -> Any:
    doc = await rk.read_json(ctx.redis, rk.TAPE)
    if doc is None:
        raise RuntimeError("no tape in store — engine has not written yet")
    return doc


@tool(
    "levels",
    {
        "description": (
            "Key levels with distance from last and status: PDH/PDL, PWH/PWL, overnight "
            "high/low, 4H swings (multi-touch flagged), gamma walls. Status is one of "
            "swept / untested / defending."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)
async def levels(ctx: ToolContext, args: dict[str, Any]) -> Any:
    doc = await rk.read_json(ctx.redis, rk.LEVELS)
    if doc is None:
        raise RuntimeError("no levels in store — engine has not written yet")
    return doc


@tool(
    "regime",
    {
        "description": (
            "Regime snapshot: gamma sign and magnitude (net GEX and its trailing percentile), "
            "realized vol, and flow-regime probabilities (AF/QUIET/CHOP from the persisted GMM "
            "— the repo's classifier; there is no trend/range/reversal day-type model)."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)
async def regime(ctx: ToolContext, args: dict[str, Any]) -> Any:
    doc = await rk.read_json(ctx.redis, rk.REGIME)
    if doc is None:
        raise RuntimeError("no regime in store — engine has not written yet")
    return doc
