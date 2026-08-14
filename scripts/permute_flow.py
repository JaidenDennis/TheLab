"""Permutation control (spec 10.9): destroy the aggressor-sign information,
keep everything else, and measure whether the edge survives.

Per permutation seed: every minute's buy/sell volumes are swapped with
probability 0.5 (magnitudes preserved, systematic direction destroyed),
decision files are refit walk-forward on the permuted flow, and the
declared variant reruns. If the real EV does not exceed (nearly) all
permuted EVs, the edge was never flow-directional.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

SWAP_KEYS = [("buy", "sell"), ("buy_ge3", "sell_ge3"), ("buy_ge5", "sell_ge5"),
             ("buy_ge10", "sell_ge10"), ("buy_ge20", "sell_ge20")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    for path in sorted(args.flow.glob("*.json")):
        session = json.loads(path.read_text())
        for minute in session["minutes"].values():
            if rng.random() < 0.5:
                for a, b in SWAP_KEYS:
                    minute[a], minute[b] = minute[b], minute[a]
        (args.out / path.name).write_text(json.dumps(session), encoding="utf-8")


if __name__ == "__main__":
    main()
