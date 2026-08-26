"""A8 — levels and structure on top of the engine's LevelBook snapshot.
Level identities/statuses come from state/levels.py; this module adds the
session-built structure (opening range, IB) and the geometry reads."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)


def _rth_bars(bars_1m: list[dict], first_min: int) -> list[dict]:
    out = []
    for b in bars_1m:
        try:
            et = datetime.fromisoformat(b["t"]).astimezone(ET)
        except (ValueError, KeyError):
            continue
        mins = (et.hour - 9) * 60 + et.minute - 30
        # bar t is the CLOSE time: the first RTH bar closes 09:31 → mins 1..first_min
        if et.time() > RTH_OPEN and mins <= first_min:
            out.append(b)
    return out


def opening_range(bars_1m: list[dict], minutes: int) -> dict[str, Any] | None:
    """A8.4 — first N minutes' high/low."""
    window = _rth_bars(bars_1m, minutes)
    if not window:
        return None
    return {
        "minutes": minutes,
        "high": max(b["h"] for b in window),
        "low": min(b["l"] for b in window),
        "complete": len(window) >= minutes,
    }


def initial_balance(bars_1m: list[dict], ib_min: int, ib_ranges_20d: list[float] | None = None) -> dict[str, Any] | None:
    """A8.5 — first-hour high/low, and the IB range vs the 20-day median."""
    ib = opening_range(bars_1m, ib_min)
    if ib is None:
        return None
    rng = ib["high"] - ib["low"]
    out = {"high": ib["high"], "low": ib["low"], "range_pts": round(rng, 2), "complete": ib["complete"]}
    if ib_ranges_20d:
        med = sorted(ib_ranges_20d)[len(ib_ranges_20d) // 2]
        out["vs_median"] = "narrow" if rng < 0.7 * med else "wide" if rng > 1.3 * med else "normal"
    return out


def nearest_levels(levels: list[dict], last: float) -> dict[str, Any]:
    """A8.8 — next level above and below, per the whole book: room to run."""
    above = [lv for lv in levels if lv.get("price") is not None and lv["price"] > last]
    below = [lv for lv in levels if lv.get("price") is not None and lv["price"] < last]
    up = min(above, key=lambda lv: lv["price"]) if above else None
    dn = max(below, key=lambda lv: lv["price"]) if below else None
    return {
        "above": {**up, "distance_pts": round(up["price"] - last, 2)} if up else None,
        "below": {**dn, "distance_pts": round(last - dn["price"], 2)} if dn else None,
    }


def confluence(levels: list[dict], price: float, band_pts: float) -> dict[str, Any]:
    """A8.9 — distinct level KINDS within the band of `price`: cluster strength."""
    near = [lv for lv in levels if lv.get("price") is not None and abs(lv["price"] - price) <= band_pts]
    kinds = sorted({lv["kind"] for lv in near})
    return {"price": price, "count": len(kinds), "kinds": kinds, "levels": [lv["name"] for lv in near]}


def round_numbers(last: float, steps: list[int]) -> list[dict[str, Any]]:
    """A8.11 — nearest round number for each step size, with distance."""
    out = []
    for step in steps:
        nearest = round(last / step) * step
        out.append({"step": step, "price": float(nearest), "distance_pts": round(abs(last - nearest), 2)})
    return out
