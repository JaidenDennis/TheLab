"""Drift audit: the live feature path vs the offline research path.

Feeds a recorded tick session through the LIVE code path (ReplayFeed tick
tap -> FlowEngine) and compares every decision bar against the offline
walk-forward decision file. Any f1_5 disagreement is a bug in the shared
layer; z_vol may differ only by the calibration source, which the audit
prints. Run after each shadow session (against that session's persisted
ticks) and before shadow start (against a known fixture).

Usage:
  uv run python scripts/audit_drift.py --ticks var/fixtures/trades/2026-08-11.jsonl \
      --decisions var/decisions/k3m/2026-08-11.json --calibration var/calibration.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nq_agent.feed.replay import ReplayFeed
from nq_agent.flow import FlowEngine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    args = parser.parse_args()

    calibration = json.loads(args.calibration.read_text())
    book: dict[str, dict] = {}
    engine = FlowEngine(calibration, book)
    for tick in ReplayFeed(args.ticks, "NQ")._ticks():
        engine.on_tick(tick)
    engine.finish_session()

    offline = json.loads(args.decisions.read_text())
    session = args.ticks.stem
    live_bars = book[session]["bars"]
    offline_bars = offline["bars"]

    f1_max = 0.0
    zvol_max = 0.0
    missing = 0
    for index, off in offline_bars.items():
        live = live_bars.get(index)
        if live is None:
            missing += 1
            continue
        # Offline files store f1_5 rounded to 5 decimals; compare at the
        # stored precision -- the live value must round to the same number.
        f1_max = max(f1_max, abs(round(live["f1_5"], 5) - off["f1_5"]))
        zvol_max = max(zvol_max, abs(live["z_vol"] - off["z_vol"]))
    print(
        f"{session}: {len(offline_bars)} offline bars, "
        f"{len(live_bars)} live bars, missing {missing}"
    )
    print(f"max |f1_5 drift| = {f1_max:.6f}   max |z_vol drift| = {zvol_max:.3f}")
    if f1_max > 1e-9 or missing:
        print("DRIFT: the live path does not reproduce the research features.")
        raise SystemExit(2)
    print("f1_5 exact; z_vol differences (if any) come from the calibration window.")


if __name__ == "__main__":
    main()
