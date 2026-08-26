"""Deterministic composite scorecards (spec §13): level_read and day_read.

Pure functions — the LLM never computes factor values itself. Every factor is
returned with its tag from factors.yaml; the validated three cite studies,
everything else is labeled discretionary framework. The heuristics for the
lean are themselves framework: what is validated is each *input*, per its tag.
"""

from __future__ import annotations

from typing import Any

from .. import redis_keys as rk
from ..factors import Factor
from . import ToolContext, tool

MIN_STOP_PTS = 15.0  # MAE-1 minimum viable stop
Q_CONFIRM = 90.0  # fc_t13 confirm threshold


def _f(factors: dict[str, Factor], key: str, value: Any, note: str | None = None) -> dict[str, Any]:
    fac = factors.get(key)
    return {
        "key": key,
        "name": fac.name if fac else key,
        "tag": fac.tag if fac else "discretionary",
        "study": fac.study if fac else None,
        "value": value,
        "note": note,
    }


def _latest_flow(bars_5m: list[dict]) -> tuple[float | None, float | None]:
    """(f1_5, impulse_q) of the last closed 5m bar."""
    if not bars_5m:
        return None, None
    b = bars_5m[-1]
    return b.get("impulse"), b.get("impulse_q")


def compute_level_read(
    price: float,
    market: dict | None,
    bars_5m: list[dict],
    levels: dict | None,
    regime: dict | None,
    factors: dict[str, Factor],
) -> dict[str, Any]:
    out_factors: list[dict[str, Any]] = []

    f1, q = _latest_flow(bars_5m)
    flow_dir = None if not f1 else ("long" if f1 > 0 else "short")
    confirmed = q is not None and q >= Q_CONFIRM
    out_factors.append(
        _f(
            factors,
            "flow_impulse_qrank",
            {"f1_5": f1, "q": q, "direction": flow_dir},
            f"needs Q{Q_CONFIRM:.0f}+ to confirm" if not confirmed else "confirming",
        )
    )

    gamma_sign = (regime or {}).get("gamma_sign")
    out_factors.append(
        _f(
            factors,
            "positive_gamma_penalty",
            gamma_sign,
            "EV-negative for breakout-style entries in all sightings" if gamma_sign == "POS" else None,
        )
    )

    out_factors.append(
        _f(
            factors,
            "stop_viability",
            {
                "min_stop_pts": MIN_STOP_PTS,
                "long_stop": round(price - MIN_STOP_PTS, 2),
                "short_stop": round(price + MIN_STOP_PTS, 2),
            },
            "stops tighter than 15 pts are inside noise per MAE-1",
        )
    )

    near: list[dict[str, Any]] = []
    for lv in (levels or {}).get("levels", []):
        if lv.get("price") is None:
            continue
        dist = abs(float(lv["price"]) - price)
        if dist <= 20:
            near.append({**lv, "distance_pts": round(dist, 2)})
    near.sort(key=lambda x: x["distance_pts"])
    out_factors.append(_f(factors, "level_hierarchy", near[:5] or "no tracked level within 20 pts"))

    va = (market or {}).get("prior_value_area") or {}
    va_pos = None
    if va.get("vah") is not None and va.get("val") is not None:
        va_pos = "above VAH" if price > va["vah"] else "below VAL" if price < va["val"] else "inside value"
    out_factors.append(_f(factors, "value_area_position", va_pos))

    if q is None:
        lean, reason = "no view", "flow unavailable"
    elif not confirmed:
        lean, reason = "no-go", f"flow not confirming (Q{q:.0f} < Q{Q_CONFIRM:.0f})"
    elif gamma_sign == "POS":
        lean, reason = f"{flow_dir} (reduced)", "flow confirms but positive gamma penalizes"
    else:
        lean, reason = flow_dir or "no view", "flow confirming"

    return {"price": price, "lean": lean, "reason": reason, "factors": out_factors}


def compute_day_read(
    market: dict | None,
    bars_5m: list[dict],
    levels: dict | None,
    regime: dict | None,
    factors: dict[str, Factor],
) -> dict[str, Any]:
    m = market or {}
    last = m.get("last")
    out_factors: list[dict[str, Any]] = []
    votes: list[tuple[str, int, str]] = []  # (factor key, vote, flip description)

    cum_delta = m.get("cum_delta_rth")
    if cum_delta is not None:
        v = 1 if cum_delta > 0 else -1 if cum_delta < 0 else 0
        votes.append(("cum_delta", v, "cumulative delta crossing zero"))
    out_factors.append(_f(factors, "htf_sweep", {"cum_delta_rth": cum_delta}, "session aggressor balance"))

    vwap = m.get("vwap")
    if last is not None and vwap is not None:
        v = 1 if last > vwap else -1
        votes.append(("vwap", v, f"1m close {'below' if v > 0 else 'above'} VWAP ({vwap})"))
    out_factors.append(_f(factors, "value_area_position", {"last": last, "vwap": vwap,
                                                          "prior_value_area": m.get("prior_value_area")}))

    va = m.get("prior_value_area") or {}
    if last is not None and va.get("poc") is not None:
        v = 1 if last > va["poc"] else -1
        votes.append(("poc", v, f"trading back {'below' if v > 0 else 'above'} prior POC ({va['poc']})"))

    on_h, on_l = m.get("on_high"), m.get("on_low")
    if last is not None and on_h is not None and on_l is not None:
        v = 1 if last > on_h else -1 if last < on_l else 0
        if v:
            votes.append(("on_range", v, f"re-entering the overnight range ({on_l}–{on_h})"))
        out_factors.append(_f(factors, "level_hierarchy", {"on_high": on_h, "on_low": on_l,
                                                           "position": "above" if v == 1 else "below" if v == -1 else "inside"}))

    f1, q = _latest_flow(bars_5m)
    flow_dir = None if not f1 else ("long" if f1 > 0 else "short")
    out_factors.append(_f(factors, "flow_impulse_qrank", {"f1_5": f1, "q": q, "direction": flow_dir},
                          "confirming" if (q or 0) >= Q_CONFIRM else f"below Q{Q_CONFIRM:.0f}"))

    gamma_sign = (regime or {}).get("gamma_sign")
    out_factors.append(_f(factors, "positive_gamma_penalty", gamma_sign,
                          "expect mean-reversion pressure" if gamma_sign == "POS" else None))
    out_factors.append(_f(factors, "day_type_classifier", (regime or {}).get("flow_regime")))

    score = sum(v for _, v, _ in votes)
    n_votes = sum(1 for _, v, _ in votes if v != 0)
    if n_votes < 2 or abs(score) < max(2, n_votes - 1):
        lean = "no bias"
        flips = [d for _, v, d in votes if v != 0]
        flip = "factors disagree; a bias needs near-unanimity — watch: " + "; ".join(flips[:3]) if flips else None
    else:
        lean = "long" if score > 0 else "short"
        against = [d for _, v, d in votes if (v > 0) == (score < 0) and v != 0]
        with_ = [d for _, v, d in votes if v != 0 and (v > 0) == (score > 0)]
        flip = with_[0] if with_ else None
        flip = f"would flip on: {flip}" if flip else None
        if against:
            flip = (flip + "; already against: " + "; ".join(against)) if flip else None

    return {
        "lean": lean,
        "votes": [{"factor": k, "vote": v, "flip": d} for k, v, d in votes],
        "flip_condition": flip,
        "factors": out_factors,
        "note": "composite is discretionary framework; only tagged-validated inputs carry tested edge",
    }


async def _gather(ctx: ToolContext) -> tuple[dict | None, list[dict], dict | None, dict | None]:
    market = await rk.read_json(ctx.redis, rk.MARKET_STATE)
    bars = await rk.read_json(ctx.redis, rk.BARS_5M)
    levels = await rk.read_json(ctx.redis, rk.LEVELS)
    regime = await rk.read_json(ctx.redis, rk.REGIME)
    return market, (bars or {}).get("bars", []), levels, regime


@tool(
    "level_read",
    {
        "description": (
            "Deterministic composite scorecard for a specific price: every factor with its "
            "value and validated/discretionary tag, a lean, and stop viability per MAE-1. "
            "Computed by code, not by the model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"price": {"type": "number"}},
            "required": ["price"],
            "additionalProperties": False,
        },
    },
)
async def level_read(ctx: ToolContext, args: dict[str, Any]) -> Any:
    market, bars, levels, regime = await _gather(ctx)
    if market is None:
        raise RuntimeError("no market state — engine offline")
    return compute_level_read(float(args["price"]), market, bars, levels, regime, ctx.factors)


@tool(
    "day_read",
    {
        "description": (
            "Deterministic composite scorecard for directional day bias. Returns 'no bias' "
            "when factors disagree and states what would flip it. Computed by code, not by "
            "the model."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)
async def day_read(ctx: ToolContext, args: dict[str, Any]) -> Any:
    market, bars, levels, regime = await _gather(ctx)
    if market is None:
        raise RuntimeError("no market state — engine offline")
    return compute_day_read(market, bars, levels, regime, ctx.factors)
