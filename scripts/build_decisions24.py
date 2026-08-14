"""Walk-forward decision files for the 24h TFR experiment.

No regime model (demoted in cycle 1): flow-threshold entries only, which
is exactly the declared shadow's entry logic, generalized to the full day
with PER-BLOCK calibration -- Asia, London, RTH and the blackout block
each get their own trailing-60-session |F1_5| percentile table and volume
z-statistics, because a single 24h distribution would let only RTH ever
arm. Zero lookahead: session d's tables come from sessions strictly
before d.

Usage:
  uv run python scripts/build_decisions24.py --flow var/flow24 --out var/decisions24
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from statistics import fmean, pstdev

from nq_agent.flow import flow_over, percentile, session_block

PCTS = (55, 60, 65, 70, 75, 80, 85)
WINDOW = 60
BLOCKS = ("asia", "london", "rth", "close")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    history: dict[str, deque[list[float]]] = {b: deque(maxlen=WINDOW) for b in BLOCKS}
    vol_history: dict[str, deque[list[float]]] = {b: deque(maxlen=WINDOW) for b in BLOCKS}

    for path in sorted(args.flow.glob("*.json")):
        payload = json.loads(path.read_text())
        if payload["qa"]["excluded"]:
            continue
        minutes = {int(k): v for k, v in payload["minutes"].items()}

        # Tables for TODAY from trailing sessions only.
        tables: dict[str, dict[str, float] | None] = {}
        vol_stats: dict[str, tuple[float, float]] = {}
        for block in BLOCKS:
            samples = [v for day in history[block] for v in day]
            tables[block] = (
                {str(p): percentile(samples, p) for p in PCTS} if len(samples) >= 500 else None
            )
            vols = [v for day in vol_history[block] for v in day]
            vol_stats[block] = (fmean(vols), pstdev(vols)) if len(vols) >= 50 else (0.0, 0.0)

        bars: dict[str, dict] = {}
        today_f1: dict[str, list[float]] = {b: [] for b in BLOCKS}
        today_vol: dict[str, list[float]] = {b: [] for b in BLOCKS}
        for end in range(5, 1441, 5):
            window = [minutes[i] for i in range(end - 4, end + 1) if i in minutes]
            if not window:
                continue
            block = session_block(end)
            f1_5 = flow_over(minutes, end, 5)
            vol_5m = float(sum(m["vol"] for m in window))
            mean, sd = vol_stats[block]
            bars[str(end)] = {
                "close": float(window[-1]["close"]),
                "f1_2": round(flow_over(minutes, end, 2), 5),
                "f1_5": round(f1_5, 5),
                "f1_15": round(flow_over(minutes, end, 15), 5),
                "z_vol": 0.0 if sd == 0 else round((vol_5m - mean) / sd, 3),
                "block": block,
                "regime": None,
                "t_af": None,
                "mahal": None,
            }
            today_f1[block].append(abs(f1_5))
            today_vol[block].append(vol_5m)

        (args.out / path.name).write_text(
            json.dumps(
                {
                    "model": "flow24",
                    "mahal_cut": None,
                    "q_f1_blocks": tables,
                    "size_cut": 5,
                    "bars": bars,
                }
            ),
            encoding="utf-8",
        )
        for block in BLOCKS:
            history[block].append(today_f1[block])
            vol_history[block].append(today_vol[block])
    print("done")


if __name__ == "__main__":
    main()
