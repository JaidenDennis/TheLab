"""Signal catalog (addendum Part A) — pure functions only.

Every signal here is deterministic code over plain data structures; the LLM
never computes any of them. Tunables come from desk/signals.yaml via
load_params(). Tags (validated / discretionary / tested-negative) live in
factors.yaml — this package computes values, it never claims edge.

Shared data shapes:
  bar   — the engine's bar doc: {"t","o","h","l","c","vol","buy","sell",
          "delta","impulse","impulse_q"} (see state/engine.py)
  tick  — (ts_epoch_s: float, price: float, size: int, dir: +1|-1)
          dir is the aggressor side, +1 = lifted offer (buy)
  cells — footprint of one bar: {price: {"buy","sell","max_print"}}
  book  — depth snapshot: {"ts": epoch_s, "bids": [[price,size],...],
          "asks": [[price,size],...]} best first
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml


def load_params(path: Path) -> dict[str, Any]:
    """desk/signals.yaml → nested dict. No defaults in code: a missing key is
    a config bug and should fail loudly at the call site."""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def linreg_slope(values: Sequence[float]) -> float | None:
    """Least-squares slope per step over the sequence (index as x)."""
    n = len(values)
    if n < 2:
        return None
    mx = (n - 1) / 2.0
    my = sum(values) / n
    denom = sum((i - mx) ** 2 for i in range(n))
    if denom == 0:
        return None
    return sum((i - mx) * (values[i] - my) for i in range(n)) / denom


def pct_rank(population: Sequence[float], x: float) -> float | None:
    """Empirical percentile of x within population, 0–100."""
    if not population:
        return None
    below = sum(1 for v in population if v <= x)
    return round(100.0 * below / len(population), 1)


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0
