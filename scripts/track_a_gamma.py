"""GFM Track A: join gamma regimes onto trades that already happened.

A column-join, not a backtest: every trade in the given journals is
tagged with its session's pre-open gamma regime and the per-regime EV
split is measured against the pre-registered gate (GFM spec 5.1):

  - n >= 40 in each of NEG and POS (else: extend with shadow accrual)
  - NEG EV >= 1.5x pooled EV  AND  POS EV <= 0.5x pooled EV
  - bootstrap p < 0.10 on the NEG-POS EV difference (10,000 resamples)
  - the split must point the way the mechanism says (NEG > POS);
    a significant REVERSE split is a kill, not a curiosity

Also reports the inverse-convention recomputation (spec 6.5): if the
conclusion flips with the dealer-sign assumption, the measurement is too
fragile to trade.

Usage:
  uv run python scripts/track_a_gamma.py --regimes var/gamma/regimes.jsonl \
      --journals var/tfr/fc_t13/journal var/shadow/journal
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path


def load_regimes(path: Path) -> dict[str, dict]:
    return {
        row["date"]: row
        for row in (json.loads(line) for line in path.read_text().splitlines())
    }


def load_trades(journal_dirs: list[Path]) -> list[tuple[str, float]]:
    trades = []
    for journal_dir in journal_dirs:
        for path in sorted(journal_dir.glob("*.jsonl")):
            for line in path.read_text().splitlines():
                record = json.loads(line)
                if record.get("event") == "position_closed":
                    trades.append((path.stem, float(record["realised_pnl"])))
    return trades


def split_stats(
    trades: list[tuple[str, float]], regime_of: dict[str, str]
) -> dict[str, list[float]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for day, pnl in trades:
        buckets[regime_of.get(day, "UNTAGGED")].append(pnl)
    return buckets


def bootstrap_p(neg: list[float], pos: list[float], resamples: int = 10_000) -> float:
    """P(NEG-POS EV difference <= 0) under resampling -- one-sided, in the
    mechanism's direction."""
    rng = random.Random(7)
    observed = sum(neg) / len(neg) - sum(pos) / len(pos)
    if observed <= 0:
        return 1.0
    at_or_below_zero = 0
    for _ in range(resamples):
        neg_sample = [rng.choice(neg) for _ in neg]
        pos_sample = [rng.choice(pos) for _ in pos]
        diff = sum(neg_sample) / len(neg_sample) - sum(pos_sample) / len(pos_sample)
        at_or_below_zero += int(diff <= 0)
    return at_or_below_zero / resamples


def report(name: str, buckets: dict[str, list[float]]) -> None:
    pooled = [p for v in buckets.values() for p in v]
    pooled_ev = sum(pooled) / len(pooled) if pooled else 0.0
    print(f"\n=== {name} (pooled n={len(pooled)}, EV=${pooled_ev:,.2f}) ===")
    for regime in ("NEG", "NEUTRAL", "POS", "UNTAGGED"):
        values = buckets.get(regime, [])
        if not values:
            continue
        ev = sum(values) / len(values)
        wins = sum(1 for v in values if v > 0)
        print(
            f"  {regime:<9} n={len(values):<5} net=${sum(values):>12,.2f} "
            f"EV=${ev:>9,.2f}  win={wins / len(values):.0%}"
        )
    neg, pos = buckets.get("NEG", []), buckets.get("POS", [])
    if len(neg) >= 2 and len(pos) >= 2:
        p = bootstrap_p(neg, pos)
        neg_ev, pos_ev = sum(neg) / len(neg), sum(pos) / len(pos)
        print(f"  NEG-POS EV difference: ${neg_ev - pos_ev:,.2f}  bootstrap p={p:.4f}")
        gate_n = len(neg) >= 40 and len(pos) >= 40
        gate_split = pooled_ev > 0 and neg_ev >= 1.5 * pooled_ev and pos_ev <= 0.5 * pooled_ev
        print(
            f"  gate: n>=40 both {'PASS' if gate_n else 'FAIL'} | "
            f"split magnitudes {'PASS' if gate_split else 'FAIL'} | "
            f"p<0.10 {'PASS' if p < 0.10 else 'FAIL'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regimes", type=Path, required=True)
    parser.add_argument("--journals", type=Path, nargs="+", required=True)
    args = parser.parse_args()

    rows = load_regimes(args.regimes)
    trades = load_trades(args.journals)
    print(f"{len(trades)} trades, {len(rows)} tagged sessions")

    report("standard convention", split_stats(trades, {d: r["regime"] for d, r in rows.items()}))

    # Inverse-convention robustness: reclassify each day by the sign of the
    # inverse NetGEX (percentile machinery omitted -- sign is the load-bearing
    # part of the inverse test; a flipped conclusion here kills the program).
    inverse_of = {
        d: ("NEG" if (r.get("net_gex_inverse") or 0) < 0 else "POS")
        for d, r in rows.items()
    }
    report("inverse convention (sign-only)", split_stats(trades, inverse_of))


if __name__ == "__main__":
    main()
