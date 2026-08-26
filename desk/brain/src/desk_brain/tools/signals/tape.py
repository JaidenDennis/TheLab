"""A4 — the tape: individual prints from the live tick window.

Ticks are (ts_epoch_s, price, size, dir). The window the engine keeps is a
few minutes deep; session-scale percentiles come from rolling stats the
engine maintains alongside (see state/tickwindow.py)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import median

TICK = 0.25


def large_prints(ticks: Sequence[tuple], threshold: int, limit: int = 10) -> list[dict[str, Any]]:
    """A4.1 — prints ≥ threshold contracts, newest first."""
    out = [
        {"ts": ts, "price": p, "size": sz, "side": "buy" if d > 0 else "sell"}
        for ts, p, sz, d in ticks
        if sz >= threshold
    ]
    return sorted(out, key=lambda x: -x["ts"])[:limit]


def print_clusters(
    ticks: Sequence[tuple], threshold: int, min_prints: int, window_s: float, band_ticks: int
) -> list[dict[str, Any]]:
    """A4.2 — ≥ min_prints large prints, same side, within window_s and
    band_ticks of each other: institutional-looking activity."""
    larges = [(ts, p, sz, d) for ts, p, sz, d in ticks if sz >= threshold]
    clusters: list[dict[str, Any]] = []
    used: set[int] = set()
    band = band_ticks * TICK
    for i, (ts, p, _sz, d) in enumerate(larges):
        if i in used:
            continue
        members = [
            j
            for j, (ts2, p2, _s2, d2) in enumerate(larges)
            if j not in used and d2 == d and abs(ts2 - ts) <= window_s and abs(p2 - p) <= band
        ]
        if len(members) >= min_prints:
            used.update(members)
            vol = sum(larges[j][2] for j in members)
            clusters.append(
                {"side": "buy" if d > 0 else "sell", "price": p, "n": len(members), "volume": vol,
                 "ts": max(larges[j][0] for j in members)}
            )
    return clusters


def tape_speed(ticks: Sequence[tuple], now_s: float, window_s: float) -> float:
    """A4.3 — prints per second over the trailing window."""
    cutoff = now_s - window_s
    n = sum(1 for ts, *_ in ticks if ts >= cutoff)
    return round(n / window_s, 2)


def is_speed_spike(current_speed: float, session_speeds: Sequence[float], spike_pctile: float) -> bool:
    """A4.4 — current speed above the session's Pxx."""
    if not session_speeds:
        return False
    s = sorted(session_speeds)
    idx = min(len(s) - 1, int(len(s) * spike_pctile / 100.0))
    return current_speed > s[idx]


def aggression_at_level(ticks: Sequence[tuple], now_s: float, level: float, window_s: float) -> dict[str, Any]:
    """A4.5 — who is attacking this level: lifting vs hitting within ±1 tick
    over the trailing window."""
    cutoff = now_s - window_s
    near = [(ts, p, sz, d) for ts, p, sz, d in ticks if ts >= cutoff and abs(p - level) <= TICK]
    return {
        "level": level,
        "lifting_vol": sum(sz for _, _, sz, d in near if d > 0),
        "hitting_vol": sum(sz for _, _, sz, d in near if d < 0),
        "prints": len(near),
    }


def print_size_shift(ticks: Sequence[tuple], now_s: float, window_s: float, session_median: float | None) -> dict[str, Any] | None:
    """A4.6 — median print size in the recent window vs the session median:
    bigger players arriving or leaving."""
    cutoff = now_s - window_s
    recent = [sz for ts, _p, sz, _d in ticks if ts >= cutoff]
    med = median(recent)
    if med is None or not session_median:
        return None
    return {
        "recent_median": med,
        "session_median": session_median,
        "shift": "bigger" if med > 1.5 * session_median else "smaller" if med < 0.67 * session_median else "similar",
    }
