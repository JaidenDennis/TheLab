"""Nightly calibration for the TFR shadow harness.

From the trailing 60 persisted flow sessions (excluding today), compute
what the walk-forward research supplied per session: the |F1_5| percentile
table and the 5m-volume z-statistics. Written for the NEXT session; the
FlowEngine loads it at startup. Zero lookahead, same arithmetic as
fit_regimes (same shared helpers).

Usage (nightly, after the session's flow file is persisted):
  uv run python scripts/calibrate_flow.py --flow var/flow --out var/calibration.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import fmean, pstdev

from nq_agent.flow import flow_over, percentile

PCTS = (55, 60, 65, 70, 75, 80, 85)
WINDOW = 60


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    sessions = sorted(args.flow.glob("*.json"))[-WINDOW:]
    if len(sessions) < 10:
        raise SystemExit(f"only {len(sessions)} flow sessions; need at least 10")

    f1_abs: list[float] = []
    vols: list[float] = []
    for path in sessions:
        payload = json.loads(path.read_text())
        if payload["qa"]["excluded"]:
            continue
        minutes = {int(k): v for k, v in payload["minutes"].items()}
        for end in range(5, 391, 5):
            window = [minutes[i] for i in range(end - 4, end + 1) if i in minutes]
            if not window:
                continue
            f1_abs.append(abs(flow_over(minutes, end, 5)))
            vols.append(sum(m["vol"] for m in window))

    calibration = {
        "sessions": [p.stem for p in (sessions[0], sessions[-1])],
        "q_f1": {str(p): percentile(f1_abs, p) for p in PCTS},
        "vol_mean": fmean(vols),
        "vol_sd": pstdev(vols),
        "size_cut": 5,
    }
    args.out.write_text(json.dumps(calibration, indent=2), encoding="utf-8")
    print(
        f"calibrated from {sessions[0].stem}..{sessions[-1].stem}: "
        f"q70={calibration['q_f1']['70']:.4f} vol_mean={calibration['vol_mean']:.0f}"
    )


if __name__ == "__main__":
    main()
