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
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_END = time(16, 0)
SIZE_CUTS = (3, 5, 10, 20)


def minute_index(et_hour: int, et_minute: int) -> int:
    """1 == the minute ending 09:31; 390 == the minute ending 16:00."""
    return (et_hour - 9) * 60 + et_minute - 30


def process_session(fixture: Path) -> dict[str, object]:
    minutes: dict[int, dict[str, object]] = {}
    sizes: list[int] = []
    unknown = 0
    total = 0
    tick_rule_hits = 0
    tick_rule_checked = 0
    prev_price: Decimal | None = None
    last_direction = 0  # tick-rule state: +1 buy, -1 sell

    with fixture.open(encoding="utf-8") as handle:
        for raw in handle:
            record = json.loads(raw)
            ts = datetime.fromisoformat(record["ts"]).astimezone(ET)
            # Bucket by the minute the tick belongs to; the bar "ending at"
            # minute m contains ticks with time in [m-1, m). The index range
            # check below is the RTH filter.
            index = minute_index(ts.hour, ts.minute) + 1
            if not (1 <= index <= 390):
                continue

            price = Decimal(record["price"])
            size = int(record["size"])
            side = record.get("side", "N")
            total += 1
            sizes.append(size)

            # Tick rule: uptick = buy, downtick = sell, unchanged = carry.
            if prev_price is not None:
                if price > prev_price:
                    rule = 1
                elif price < prev_price:
                    rule = -1
                else:
                    rule = last_direction
            else:
                rule = 0
            if rule != 0:
                last_direction = rule
            prev_price = price

            if side == "B":
                direction = 1
            elif side == "A":
                direction = -1
            else:
                unknown += 1
                direction = rule  # fallback per spec section 2
            if side in ("B", "A") and rule != 0:
                tick_rule_checked += 1
                tick_rule_hits += int((1 if side == "B" else -1) == rule)

            bucket = minutes.setdefault(
                index,
                {
                    "close": None,
                    "vol": 0,
                    "buy": 0,
                    "sell": 0,
                    **{f"buy_ge{c}": 0 for c in SIZE_CUTS},
                    **{f"sell_ge{c}": 0 for c in SIZE_CUTS},
                },
            )
            bucket["close"] = str(price)
            bucket["vol"] += size  # type: ignore[operator]
            key = "buy" if direction >= 0 else "sell"
            if direction != 0:
                bucket[key] += size  # type: ignore[operator]
                for cut in SIZE_CUTS:
                    if size >= cut:
                        bucket[f"{key}_ge{cut}"] += size  # type: ignore[operator]

    sizes.sort()

    def pct(p: float) -> int:
        return sizes[min(len(sizes) - 1, int(len(sizes) * p))] if sizes else 0

    unknown_share = unknown / total if total else 1.0
    agreement = tick_rule_hits / tick_rule_checked if tick_rule_checked else 0.0
    return {
        "qa": {
            "ticks": total,
            "unknown_share": round(unknown_share, 5),
            "tick_rule_agreement": round(agreement, 4),
            "size_p50": pct(0.50),
            "size_p90": pct(0.90),
            "size_p95": pct(0.95),
            "size_p99": pct(0.99),
            "excluded": unknown_share > 0.05,
        },
        "minutes": {str(k): v for k, v in sorted(minutes.items())},
    }


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
