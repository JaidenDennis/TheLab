"""A6 — volume profile and value. Prior/developing value areas come from the
engine (state/pricebins.py, state/levels.py); this module derives the reads
on top of them plus multi-day structure from bar history."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

TICK = 0.25


def position_vs_value(last: float | None, va: dict | None) -> dict[str, Any] | None:
    """A6.3 — above / inside / below prior value, with distance to the edge."""
    if last is None or not va or va.get("vah") is None or va.get("val") is None:
        return None
    if last > va["vah"]:
        return {"position": "above", "distance_pts": round(last - va["vah"], 2)}
    if last < va["val"]:
        return {"position": "below", "distance_pts": round(va["val"] - last, 2)}
    return {"position": "inside", "distance_pts": 0.0}


def value_migration(developing: dict | None, prior: dict | None) -> str | None:
    """A6.4 — developing POC vs prior POC: higher / lower / overlapping."""
    if not developing or not prior or developing.get("poc") is None or prior.get("poc") is None:
        return None
    diff = developing["poc"] - prior["poc"]
    if abs(diff) <= 2 * TICK:
        return "overlapping"
    return "higher" if diff > 0 else "lower"


def overnight_profile(on_high: float | None, on_low: float | None, on_ranges_20d: list[float]) -> dict[str, Any] | None:
    """A6.5 — ON range as % of the trailing ON-range distribution; narrow ON
    often expands."""
    if on_high is None or on_low is None:
        return None
    rng = on_high - on_low
    out: dict[str, Any] = {"on_high": on_high, "on_low": on_low, "range_pts": round(rng, 2)}
    if on_ranges_20d:
        med = sorted(on_ranges_20d)[len(on_ranges_20d) // 2]
        out["vs_median"] = "narrow" if rng < 0.7 * med else "wide" if rng > 1.3 * med else "normal"
    return out


def bar_profile(bars: list[dict]) -> dict[float, float]:
    """Bar-range volume approximation (same method as state/levels.py) over
    arbitrary bars — the composite profile's raw material."""
    profile: dict[float, float] = defaultdict(float)
    for b in bars:
        lo, hi = float(b["l"]), float(b["h"])
        n = max(1, int(round((hi - lo) / TICK)) + 1)
        share = (b.get("vol") or 0) / n
        for i in range(n):
            profile[round(lo + i * TICK, 2)] += share
    return dict(profile)


def composite_nodes(profile: dict[float, float], near: float, band_pts: float, hvn_mult: float, lvn_mult: float) -> dict[str, list[float]]:
    """A6.6 — multi-day HVN/LVN within band_pts of `near`: structural magnets
    and air pockets."""
    if not profile:
        return {"hvn": [], "lvn": []}
    mean = sum(profile.values()) / len(profile)
    hvn = [p for p, v in profile.items() if v > hvn_mult * mean and abs(p - near) <= band_pts]
    lvn = [p for p, v in profile.items() if v < lvn_mult * mean and abs(p - near) <= band_pts]
    return {"hvn": sorted(hvn), "lvn": sorted(lvn)}


def poor_extremes(bars: list[dict]) -> dict[str, bool]:
    """A6.7 — poor/unfinished session extremes: the high (low) set by more
    than one bar within a tick = no taper, likely revisit."""
    if not bars:
        return {"poor_high": False, "poor_low": False}
    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)
    hi_touches = sum(1 for b in bars if hi - b["h"] <= TICK)
    lo_touches = sum(1 for b in bars if b["l"] - lo <= TICK)
    return {"poor_high": hi_touches >= 2, "poor_low": lo_touches >= 2}


def acceptance(bars: list[dict], level: float, min_bars: int) -> dict[str, Any] | None:
    """A6.8 — is a break real: consecutive closes beyond `level` ending at the
    latest bar. `accepted` above/below after min_bars, else 'rejected' if the
    excursion came straight back."""
    if not bars:
        return None
    side = None
    run = 0
    for b in reversed(bars):
        s = "above" if b["c"] > level else "below" if b["c"] < level else None
        if side is None:
            side = s
        if s != side or s is None:
            break
        run += 1
    if side is None:
        return None
    return {"level": level, "side": side, "bars_beyond": run, "accepted": run >= min_bars}
