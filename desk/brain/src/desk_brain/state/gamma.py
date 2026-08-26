"""Gamma regime + walls from the repo's persisted gamma artifacts.

Reads var/gamma/regimes.jsonl (one row per session, written by
scripts/snapshot_gamma.py) and rebuilds per-strike GEX from the archived raw
chain using nq_agent.gex's own public functions — walls are net-new but every
number is derived from the same math the study used.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nq_agent.gex import ChainEntry, bs_gamma, implied_vol

log = logging.getLogger(__name__)


def latest_regime_row(gamma_dir: Path) -> dict[str, Any] | None:
    path = gamma_dir / "regimes.jsonl"
    if not path.exists():
        return None
    last: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
    return last


def trailing_percentile(gamma_dir: Path, net_gex: float, window: int = 120) -> float | None:
    path = gamma_dir / "regimes.jsonl"
    if not path.exists():
        return None
    values: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        v = row.get("net_gex")
        if isinstance(v, (int, float)):
            values.append(float(v))
    tail = values[-window:]
    if not tail:
        return None
    return round(100.0 * sum(1 for v in tail if v <= net_gex) / len(tail), 1)


def gamma_walls(
    gamma_dir: Path,
    date: str,
    spot: float,
    *,
    r: float = 0.04,
    q: float = 0.006,
    dealer_sign_calls: int = 1,
    top_n: int = 4,
) -> list[dict[str, Any]]:
    """Top |per-strike GEX| strikes near spot from the raw chain archive."""
    path = gamma_dir / "raw" / f"{date}.json"
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    by_strike: dict[float, float] = {}
    for row in doc.get("chain", []):
        try:
            entry = ChainEntry(
                strike=float(row[0]),
                expiry_days=float(row[1]),
                is_call=bool(row[2]),
                open_interest=int(row[3]),
                mid=float(row[4]),
            )
        except (TypeError, ValueError, IndexError):
            continue
        if entry.open_interest <= 0 or entry.expiry_days <= 0 or entry.mid <= 0:
            continue
        t_years = entry.expiry_days / 365.0
        vol = implied_vol(entry.mid, spot, entry.strike, t_years, entry.is_call, r, q)
        if vol is None:
            continue
        gamma = bs_gamma(spot, entry.strike, t_years, vol, r, q)
        sign = dealer_sign_calls if entry.is_call else -dealer_sign_calls
        by_strike[entry.strike] = by_strike.get(entry.strike, 0.0) + gamma * entry.open_interest * 100 * spot * sign

    # nearby strikes only (±8% of spot) — far wings aren't tradeable walls
    near = {k: v for k, v in by_strike.items() if abs(k - spot) / spot <= 0.08}
    top = sorted(near.items(), key=lambda kv: -abs(kv[1]))[:top_n]
    return [
        {"strike": strike, "gex": round(gex, 0), "side": "support" if gex > 0 else "resistance"}
        for strike, gex in sorted(top)
    ]


def flow_regime_today(decisions_dir: Path, date: str) -> dict[str, Any] | None:
    """AF/QUIET/CHOP labels for today if the offline research pipeline produced them.

    The live engine deliberately emits regime=None (demoted in cycle 1), so this
    is best-effort: present when fit_regimes has run for the date, else None.
    """
    path = decisions_dir / f"{date}.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    bars = doc.get("bars") or {}
    labels = [b.get("regime") for b in bars.values() if b.get("regime")]
    if not labels:
        return None
    counts = {lab: labels.count(lab) for lab in set(labels)}
    return {"labels_seen": counts, "latest": labels[-1], "model": doc.get("model")}
