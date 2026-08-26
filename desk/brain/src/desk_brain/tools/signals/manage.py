"""A12 — live trade management math. Everything here is arithmetic on the
open position + bars since entry; stop viability (12.2) is the one validated
item (MAE-1) and its threshold lives in signals.yaml under manage."""

from __future__ import annotations

from typing import Any


def _direction(side: str) -> int:
    return 1 if side.lower() in ("long", "buy") else -1


def live_excursions(entry_price: float, side: str, bars_since_entry: list[dict], last: float | None) -> dict[str, Any] | None:
    """A12.1 — MAE/MFE since entry, in points, from bar extremes (bar
    resolution understates true tick excursions slightly; graded the same
    way as the nightly MAE-1 race)."""
    d = _direction(side)
    if not bars_since_entry and last is None:
        return None
    highs = [b["h"] for b in bars_since_entry] + ([last] if last is not None else [])
    lows = [b["l"] for b in bars_since_entry] + ([last] if last is not None else [])
    favorable = (max(highs) - entry_price) * d if d > 0 else (entry_price - min(lows))
    adverse = (entry_price - min(lows)) if d > 0 else (max(highs) - entry_price)
    return {"mfe_pts": round(max(0.0, favorable), 2), "mae_pts": round(max(0.0, adverse), 2)}


def stop_viability(entry_or_last: float, stop_price: float | None, min_stop_pts: float) -> dict[str, Any]:
    """A12.2 — VALIDATED (MAE-1): a stop closer than min_stop_pts sits inside
    noise and gets hit by it."""
    if stop_price is None:
        return {"has_stop": False, "min_stop_pts": min_stop_pts, "viable": None,
                "note": "no working stop found"}
    dist = abs(entry_or_last - stop_price)
    return {"has_stop": True, "stop": stop_price, "distance_pts": round(dist, 2),
            "min_stop_pts": min_stop_pts, "viable": dist >= min_stop_pts}


def r_multiple(entry_price: float, side: str, last: float | None, stop_price: float | None) -> float | None:
    """A12.3 — unrealized P&L ÷ initial risk (distance to stop)."""
    if last is None or stop_price is None:
        return None
    d = _direction(side)
    risk = abs(entry_price - stop_price)
    if risk <= 0:
        return None
    return round((last - entry_price) * d / risk, 2)


def flow_since_entry(bars_since_entry: list[dict], side: str) -> dict[str, Any] | None:
    """A12.4 — net delta and best impulse Q since entry: is the flow that got
    you in still with you."""
    if not bars_since_entry:
        return None
    d = _direction(side)
    net = sum(b.get("delta") or 0 for b in bars_since_entry)
    qs = [b["impulse_q"] for b in bars_since_entry if b.get("impulse_q") is not None]
    imps = [b.get("impulse") or 0 for b in bars_since_entry]
    with_you = net * d > 0
    return {"net_delta": net, "with_position": with_you,
            "best_q": max(qs) if qs else None,
            "last_impulse_sign": "buy" if imps and imps[-1] > 0 else "sell" if imps and imps[-1] < 0 else None}


def distances(last: float | None, stop_price: float | None, target_price: float | None) -> dict[str, Any]:
    """A12.5 — proximity to stop and target, points."""
    return {
        "to_stop_pts": round(abs(last - stop_price), 2) if last is not None and stop_price is not None else None,
        "to_target_pts": round(abs(target_price - last), 2) if last is not None and target_price is not None else None,
    }


def time_in_trade(minutes: float, journal_median_min: float | None, overstay_mult: float) -> dict[str, Any]:
    """A12.7 — minutes in, vs the journal median for this setup when known."""
    out: dict[str, Any] = {"minutes": round(minutes, 1), "overstaying": None}
    if journal_median_min and journal_median_min > 0:
        out["journal_median_min"] = journal_median_min
        out["overstaying"] = minutes > overstay_mult * journal_median_min
    return out


def plan_alignment(side: str, plan_bias: str | None) -> dict[str, Any] | None:
    """A12.8 — position direction vs the session plan's bias."""
    if not plan_bias:
        return None
    bias = plan_bias.strip().lower()
    if bias in ("two-sided", "neutral", "no bias", "none"):
        return {"on_plan": True, "note": "plan is two-sided"}
    aligned = (bias.startswith("long") and _direction(side) > 0) or (bias.startswith("short") and _direction(side) < 0)
    return {"on_plan": aligned, "plan_bias": plan_bias}
