"""Continuous empirical Q-rank for |f1_5| flow impulse.

The persisted q_f1 tables (var/calibration.json, var/decisions) stop at P85,
but factors.yaml's confirm threshold is Q90 — so the brain rebuilds the full
empirical CDF from the raw per-session flow files, using the engine's own
flow_over so the metric is bit-identical with fc_t13 (nq_agent/flow.py).
"""

from __future__ import annotations

import bisect
import json
import logging
from pathlib import Path

from nq_agent.flow import flow_over

log = logging.getLogger(__name__)


class QRank:
    def __init__(self, samples: list[float]):
        self.samples = sorted(samples)

    @classmethod
    def from_flow_store(cls, flow_dir: Path) -> "QRank":
        samples: list[float] = []
        files = sorted(flow_dir.glob("*.json"))
        used = 0
        for path in files:
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if doc.get("partial") or (doc.get("qa") or {}).get("excluded"):
                continue
            minutes = {int(k): v for k, v in (doc.get("minutes") or {}).items()}
            if not minutes:
                continue
            used += 1
            top = max(minutes)
            for end in range(5, top + 1, 5):
                f = flow_over(minutes, end, 5)
                if f != 0.0:
                    samples.append(abs(f))
        log.info("qrank: %d samples from %d/%d sessions in %s", len(samples), used, len(files), flow_dir)
        return cls(samples)

    def rank(self, f1: float | None) -> float | None:
        """Percentile rank (0–100) of |f1| in the reference distribution."""
        if f1 is None or not self.samples:
            return None
        pos = bisect.bisect_right(self.samples, abs(f1))
        return round(100.0 * pos / len(self.samples), 1)
