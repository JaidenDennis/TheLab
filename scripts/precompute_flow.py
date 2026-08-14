"""TFR pass 1: tick fixtures -> per-session flow aggregates, with QA.

Reads trades-schema fixtures ({ts, price, size, side}) and emits one JSON
per session containing per-1-minute RTH aggregates:

  close, volume, buy_vol, sell_vol            (aggressor-signed volume)
  buy_ge{N}, sell_ge{N} for N in {3,5,10,20}  (large-trade slices; pass 2
                                               picks the cutoff nearest the
                                               trailing-20-session P95 size)

plus a session QA block: % unknown aggressor side, tick-rule agreement,
and trade-size percentiles. Sessions with >5% unknown side are still
written but flagged excluded=true (spec section 2's data-quality gate).

Side convention (Databento trades schema): 'B' = buy aggressor, 'A' =
sell aggressor, 'N' = unknown. The tick-rule agreement stat exists to
catch a convention error loudly: if the flag were inverted, agreement
with the tick rule would collapse below 50%, and pass 2 refuses sessions
from a run whose median agreement looks inverted. Unknown-side trades
fall back to the tick rule.

Everything here is per-session and lookahead-free by construction; all
cross-session work (z-scores, percentile thresholds, regime fits) lives
in pass 2 (scripts/fit_regimes.py).

Usage:
  uv run python scripts/precompute_flow.py --ticks var/fixtures/trades-dev \
      --out var/flow --start 2021-01-04 --end 2024-09-30
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from nq_agent.flow import MinuteFlowAggregator
from nq_agent.models import Tick


def process_session(fixture: Path) -> dict[str, object]:
    """One session through the SHARED aggregator (nq_agent.flow) -- the same
    code the live shadow harness runs, which is the whole point."""
    aggregator = MinuteFlowAggregator()
    with fixture.open(encoding="utf-8") as handle:
        for raw in handle:
            record = json.loads(raw)
            aggregator.on_tick(
                Tick(
                    symbol="NQ",
                    ts=datetime.fromisoformat(record["ts"]),
                    price=Decimal(record["price"]),
                    size=int(record["size"]),
                    side=record.get("side"),
                )
            )
    return aggregator.session_payload()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2000, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2100, 1, 1))
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    fixtures = [
        p
        for p in sorted(args.ticks.glob("*.jsonl"))
        if args.start <= date.fromisoformat(p.stem) <= args.end
    ]
    print(f"{len(fixtures)} sessions")
    excluded = 0
    agreements: list[float] = []
    for fixture in fixtures:
        out_path = args.out / f"{fixture.stem}.json"
        if out_path.exists():
            continue
        session = process_session(fixture)
        out_path.write_text(json.dumps(session), encoding="utf-8")
        qa = session["qa"]
        agreements.append(qa["tick_rule_agreement"])  # type: ignore[index]
        excluded += int(qa["excluded"])  # type: ignore[index]
        print(
            f"{fixture.stem}: {qa['ticks']:>9,} ticks  "  # type: ignore[index]
            f"unknown {qa['unknown_share']:.2%}  agree {qa['tick_rule_agreement']:.1%}"  # type: ignore[index]
        )
    if agreements:
        agreements.sort()
        median = agreements[len(agreements) // 2]
        print(f"\nmedian tick-rule agreement: {median:.1%}  excluded sessions: {excluded}")
        if median < 0.5:
            print("ERROR: agreement below 50% -- the side convention is likely inverted.")
            raise SystemExit(2)


if __name__ == "__main__":
    main()
