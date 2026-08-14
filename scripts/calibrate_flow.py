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

from nq_agent.flow import compute_calibration


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    calibration = compute_calibration(args.flow)
    args.out.write_text(json.dumps(calibration, indent=2), encoding="utf-8")
    print(
        f"calibrated from {calibration['sessions'][0]}..{calibration['sessions'][1]}: "
        f"q70={calibration['q_f1']['70']:.4f} vol_mean={calibration['vol_mean']:.0f}"
    )


if __name__ == "__main__":
    main()
