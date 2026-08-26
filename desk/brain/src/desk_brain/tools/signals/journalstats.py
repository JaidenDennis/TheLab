"""A13 — journal-derived stats, pure part. The tool wrappers query Supabase
and hand plain trade rows here; nothing in this module touches the network.
Always report n — below min_n these are anecdotes, not statistics."""

from __future__ import annotations

from typing import Any

from . import median


def setup_stats(trades: list[dict], min_n: int) -> dict[str, Any]:
    """A13.1 — win rate / expectancy / n over the given (already filtered)
    trades. The caller chooses the facet filter; this just counts honestly."""
    pnls = [float(t["net_pnl"]) for t in trades if t.get("net_pnl") is not None]
    n = len(pnls)
    if n == 0:
        return {"n": 0, "win_rate": None, "expectancy": None, "reliable": False}
    wins = [p for p in pnls if p > 0]
    return {
        "n": n,
        "win_rate": round(len(wins) / n, 2),
        "expectancy": round(sum(pnls) / n, 2),
        "median_pnl": median(pnls),
        "reliable": n >= min_n,
    }


def violation_rate(entries: list[dict], lookback: int) -> dict[str, Any]:
    """A13.2 — rule violations by rule over the last `lookback` checklist
    entries (rows carry rule_violations: list[str])."""
    recent = entries[-lookback:]
    by_rule: dict[str, int] = {}
    violated = 0
    for e in recent:
        rules = e.get("rule_violations") or []
        if rules:
            violated += 1
        for r in rules:
            by_rule[r] = by_rule.get(r, 0) + 1
    return {"trades": len(recent), "trades_with_violation": violated, "by_rule": by_rule}


def median_hold_minutes(trades: list[dict]) -> float | None:
    """Median time-in-trade for the given trades (entry_at/exit_at ISO)."""
    from datetime import datetime

    mins: list[float] = []
    for t in trades:
        try:
            a = datetime.fromisoformat(str(t["entry_at"]))
            b = datetime.fromisoformat(str(t["exit_at"]))
        except (KeyError, ValueError, TypeError):
            continue
        mins.append((b - a).total_seconds() / 60.0)
    return round(median(mins), 1) if mins else None


def similar_trades(trades: list[dict], price: float, band_pts: float, n: int) -> list[dict]:
    """A13.5 — most recent trades entered within band_pts of `price`."""
    near = [t for t in trades if t.get("entry_price") is not None and abs(float(t["entry_price"]) - price) <= band_pts]
    near.sort(key=lambda t: str(t.get("entry_at") or ""), reverse=True)
    return near[:n]
