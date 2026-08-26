"""A7 — VWAP family. The engine's session VWAP is tick-true; the series and
bands here are rebuilt from 1m bars (typical price × volume) so they are
computable at read time — close enough for bands and slope, and the last
point is reconciled against the tick-true value by the caller."""

from __future__ import annotations

from math import sqrt
from typing import Any

from . import linreg_slope


def vwap_series(bars: list[dict]) -> list[float]:
    """Cumulative VWAP after each bar, typical-price weighted."""
    out: list[float] = []
    pv = v = 0.0
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        vol = b.get("vol") or 0
        pv += tp * vol
        v += vol
        out.append(round(pv / v, 2) if v > 0 else b["c"])
    return out


def vwap_bands(bars: list[dict]) -> dict[str, Any] | None:
    """A7.2 — session VWAP ±1σ/±2σ (volume-weighted stdev of typical price)."""
    pv = v = 0.0
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        vol = b.get("vol") or 0
        pv += tp * vol
        v += vol
    if v <= 0:
        return None
    mean = pv / v
    var = 0.0
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        var += (b.get("vol") or 0) * (tp - mean) ** 2
    sigma = sqrt(var / v)
    return {
        "vwap": round(mean, 2),
        "sigma": round(sigma, 2),
        "upper1": round(mean + sigma, 2),
        "lower1": round(mean - sigma, 2),
        "upper2": round(mean + 2 * sigma, 2),
        "lower2": round(mean - 2 * sigma, 2),
    }


def vwap_slope(bars: list[dict], slope_bars: int) -> float | None:
    """A7.3 — slope of the VWAP series over the last `slope_bars` bars."""
    return linreg_slope(vwap_series(bars)[-slope_bars:])


def distance_to_vwap(last: float | None, bands: dict | None) -> dict[str, Any] | None:
    """A7.4 — points and σ from fair."""
    if last is None or not bands or not bands.get("sigma"):
        return None
    pts = last - bands["vwap"]
    return {"points": round(pts, 2), "sigmas": round(pts / bands["sigma"], 2)}


def vwap_test_outcome(bars: list[dict], vwap: float | None, touch_pts: float = 2.0) -> dict[str, Any] | None:
    """A7.5 — the most recent bar that touched VWAP: delta during the test and
    whether the close held the side it came from."""
    if vwap is None:
        return None
    for i in range(len(bars) - 1, -1, -1):
        b = bars[i]
        if b["l"] - touch_pts <= vwap <= b["h"] + touch_pts:
            held_above = b["c"] > vwap
            return {
                "bar_t": b.get("t"),
                "delta_at_test": b.get("delta"),
                "closed": "above" if held_above else "below",
                "bars_ago": len(bars) - 1 - i,
            }
    return None


def anchored_vwap(bars: list[dict], anchor_index: int) -> float | None:
    """A7.6 — VWAP from an anchor bar (session high/low, ON open, a marked
    point) to now: the average price of everyone in since the anchor."""
    if not 0 <= anchor_index < len(bars):
        return None
    series = vwap_series(bars[anchor_index:])
    return series[-1] if series else None


def anchor_at_extreme(bars: list[dict], which: str) -> int | None:
    """Index of the session high/low bar, for anchoring."""
    if not bars:
        return None
    if which == "high":
        return max(range(len(bars)), key=lambda i: bars[i]["h"])
    return min(range(len(bars)), key=lambda i: bars[i]["l"])
