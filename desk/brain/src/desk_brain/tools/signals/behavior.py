"""A3 — absorption, exhaustion, initiative: reading who is winning.

All discretionary framework (the SFB study tested the sweep ENTRY and found
nothing — 3.7 stays context, never a trigger)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

TICK = 0.25


def absorption(
    ticks: Sequence[tuple],
    now_s: float,
    center: float,
    band_ticks: int,
    min_vol: float,
    window_s: float,
    max_range_ticks: int,
) -> dict[str, Any] | None:
    """A3.1/3.2 — within `band_ticks` of `center` over the trailing window:
    heavy one-sided aggression with price pinned = someone passively soaking
    it up. Returns the absorbing side (the PASSIVE winner) or None."""
    half = band_ticks * TICK
    cutoff = now_s - window_s
    in_band = [(ts, p, sz, d) for ts, p, sz, d in ticks if ts >= cutoff and abs(p - center) <= half]
    if not in_band:
        return None
    buy = sum(sz for _, _, sz, d in in_band if d > 0)
    sell = sum(sz for _, _, sz, d in in_band if d < 0)
    prices = [p for _, p, _, _ in in_band]
    range_ticks = (max(prices) - min(prices)) / TICK
    aggressor_vol, aggressor_side = (buy, "buy") if buy >= sell else (sell, "sell")
    if aggressor_vol < min_vol or range_ticks > max_range_ticks:
        return None
    # score: aggression per tick of progress (A3.2, un-normalized — caller
    # percentile-ranks it against the session's other absorption events)
    score = aggressor_vol / (range_ticks + 1.0)
    return {
        "center": center,
        "aggressor_side": aggressor_side,
        "absorbing_side": "sell" if aggressor_side == "buy" else "buy",
        "aggressor_vol": round(aggressor_vol),
        "range_ticks": round(range_ticks, 1),
        "raw_score": round(score, 1),
    }


def exhaustion(bars: list[dict], falling_bars: int, final_vol_mult: float) -> dict[str, Any] | None:
    """A3.3 — a directional push whose aggressor volume per tick of progress
    falls for `falling_bars` bars, ending in a low-volume bar."""
    need = falling_bars + 1
    if len(bars) < max(need, 5):
        return None
    window = bars[-need:]
    closes = [b["c"] for b in window]
    direction = 1 if closes[-1] > closes[0] else -1
    if not all((closes[i + 1] - closes[i]) * direction >= 0 for i in range(len(closes) - 1)):
        return None  # not a one-way push

    def vol_per_tick(b: dict) -> float:
        progress = max(abs(b["c"] - b["o"]) / TICK, 1.0)
        side_vol = b.get("buy" if direction > 0 else "sell") or 0
        return side_vol / progress

    vpt = [vol_per_tick(b) for b in window]
    if not all(vpt[i + 1] < vpt[i] for i in range(len(vpt) - 1)):
        return None
    mean_vol = sum(b.get("vol") or 0 for b in bars) / len(bars)
    if (window[-1].get("vol") or 0) >= final_vol_mult * mean_vol:
        return None
    return {"direction": "up" if direction > 0 else "down", "bars": falling_bars, "read": "push running out of participants"}


def initiative_vs_responsive(price: float, delta: float | None, prior_va: dict | None) -> str | None:
    """A3.4 — outside prior value with same-sign delta = initiative (trend
    players driving); inside value or fading the edge = responsive."""
    if delta is None or not prior_va or prior_va.get("vah") is None or prior_va.get("val") is None:
        return None
    if price > prior_va["vah"]:
        return "initiative" if delta > 0 else "responsive"
    if price < prior_va["val"]:
        return "initiative" if delta < 0 else "responsive"
    return "responsive"


def effort_vs_result(bars: list[dict]) -> dict[str, Any] | None:
    """A3.5 — last bar's volume percentile vs range percentile across the
    given bars. High effort + small result = someone leaning against it."""
    if len(bars) < 5:
        return None
    vols = [b.get("vol") or 0 for b in bars]
    ranges = [b["h"] - b["l"] for b in bars]
    last_v, last_r = vols[-1], ranges[-1]
    vol_pct = 100.0 * sum(1 for v in vols if v <= last_v) / len(vols)
    rng_pct = 100.0 * sum(1 for r in ranges if r <= last_r) / len(ranges)
    flag = vol_pct >= 70 and rng_pct <= 30
    return {"volume_pctile": round(vol_pct), "range_pctile": round(rng_pct), "high_effort_small_result": flag}


def trapped_traders(stack: dict[str, Any], later_bars: list[dict], confirm_bars: int) -> dict[str, Any] | None:
    """A3.6 — a stacked-imbalance zone (2.3) that price then trades through
    and closes beyond within `confirm_bars` bars: those aggressors are
    underwater and will fuel the move away."""
    for b in later_bars[:confirm_bars]:
        if stack["side"] == "buy" and b["c"] < stack["low"]:
            return {"trapped": "buyers", "zone": [stack["low"], stack["high"]], "closed_at": b["c"]}
        if stack["side"] == "sell" and b["c"] > stack["high"]:
            return {"trapped": "sellers", "zone": [stack["low"], stack["high"]], "closed_at": b["c"]}
    return None


def failed_breakout(bars: list[dict], level: float, beyond_ticks: int, return_bars: int) -> dict[str, Any] | None:
    """A3.7 — traded beyond `level` by ≥ beyond_ticks and closed back inside
    within `return_bars`. Sweep + reclaim; the ENTRY side tested negative
    (SFB) — this is context only. Returns the most recent COMPLETED failed
    breakout (an excursion still living beyond the level isn't failed yet)."""
    threshold = beyond_ticks * TICK
    for i in range(len(bars) - 1, -1, -1):
        b = bars[i]
        above = b["h"] >= level + threshold and b["o"] <= level
        below = b["l"] <= level - threshold and b["o"] >= level
        if not (above or below):
            continue
        for j in range(i, min(i + return_bars + 1, len(bars))):
            back = bars[j]["c"] < level if above else bars[j]["c"] > level
            if back:
                return {
                    "side": "high" if above else "low",
                    "level": level,
                    "excursion_delta": bars[i].get("delta"),
                    "reclaimed_at_bar": j - i,
                    "note": "sweep entries carry no edge (SFB) — context only",
                }
    return None


def reversal_delta(sweep_bar: dict, next_bar: dict, mult: float) -> bool:
    """A3.8 — next bar's delta flips sign and exceeds mult× the sweep bar's."""
    sd, nd = sweep_bar.get("delta") or 0, next_bar.get("delta") or 0
    return sd * nd < 0 and abs(nd) >= mult * abs(sd)


def continuation_delta(break_bar: dict, retest_bar: dict, mult: float) -> bool:
    """A3.9 — retest bar holds with same-sign delta ≥ mult× the break bar's."""
    bd, rd = break_bar.get("delta") or 0, retest_bar.get("delta") or 0
    return bd * rd > 0 and abs(rd) >= mult * abs(bd)
