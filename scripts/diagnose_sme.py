"""Replay every closed SME trade against its session's bars and slice the EV.

Answers the question a P&L line cannot: WHERE does the strategy lose --
and, for the winners it did have, whether the exits captured the tail the
thesis depends on or amputated it. For each trade:

  - R multiples: realised, max favourable excursion (MFE) to exit, MFE to
    the 15:55 deadline, max adverse excursion (MAE) to exit
  - a counterfactual exit: hold from entry with the INITIAL stop only,
    flat at 15:55 -- no break-even move, no trail. If this beats the real
    exits, the trail is cutting tails; if it loses more, the trail is
    saving money and the entries themselves are the problem.

Cuts: year, direction, entry hour (ET), day of week, exit reason, entry
number within the day, month equity series, R-multiple reach rates.

Usage:
  uv run python scripts/diagnose_sme.py --journal var/ablations/a_b/journal \
      --fixtures var/fixtures/1m --out var/ablations/a_b-diagnostics.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nq_agent.feed.aggregator import BarAggregator
from nq_agent.feed.replay import ReplayFeed
from nq_agent.models import Bar

ET = ZoneInfo("America/New_York")
POINT_VALUE = Decimal("20")
COMMISSION_RT = Decimal("10")
DEADLINE = time(15, 55)


def one_minute_bars(fixture: Path) -> list[Bar]:
    aggregator = BarAggregator("NQ", ["1m"])
    bars: list[Bar] = []
    for tick in ReplayFeed(fixture, "NQ")._ticks():
        bars.extend(aggregator.add_tick(tick))
    bars.extend(b for b in aggregator.flush() if b.closed)
    return bars


def load_trades(journal_dir: Path) -> list[dict[str, Any]]:
    """position_closed records joined to their ENTRY signal (for the stop)
    via the signal timestamp == position entry_time identity."""
    trades = []
    for path in sorted(journal_dir.glob("*.jsonl")):
        entries_by_ts: dict[str, dict[str, Any]] = {}
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if record.get("event") == "signal_emitted" and record.get("intent") == "ENTRY":
                # The journal stamp is the sim clock at the signal's bar close,
                # which is also the position's entry_time -- the join key.
                entries_by_ts[record["ts"]] = record
            elif record.get("event") == "position_closed":
                record["session"] = path.stem
                record["signal"] = entries_by_ts.get(record["entry_time"])
                trades.append(record)
    return trades


def excursions(
    bars: list[Bar],
    direction: str,
    entry_price: Decimal,
    stop: Decimal | None,
    entry_time: datetime,
    exit_time: datetime,
) -> dict[str, Any]:
    long = direction == "LONG"
    sign = 1 if long else -1
    mfe_exit = Decimal("0")
    mae_exit = Decimal("0")
    mfe_deadline = Decimal("0")
    alt_exit_points: Decimal | None = None

    for bar in bars:
        if bar.close_time <= entry_time:
            continue
        bar_et = bar.close_time.astimezone(ET).time()
        if bar_et > DEADLINE:
            break
        favourable = (bar.high - entry_price) if long else (entry_price - bar.low)
        adverse = (entry_price - bar.low) if long else (bar.high - entry_price)
        if bar.close_time <= exit_time:
            mfe_exit = max(mfe_exit, favourable)
            mae_exit = max(mae_exit, adverse)
        mfe_deadline = max(mfe_deadline, favourable)

        # Counterfactual: initial stop only, flat at the deadline.
        if alt_exit_points is None and stop is not None:
            stopped = bar.low <= stop if long else bar.high >= stop
            if stopped:
                gapped = bar.open < stop if long else bar.open > stop
                fill = bar.open if gapped else stop
                alt_exit_points = (fill - entry_price) * sign
            elif bar_et >= DEADLINE:
                alt_exit_points = (bar.close - entry_price) * sign

    if alt_exit_points is None and stop is not None and bars:
        last = bars[-1]
        alt_exit_points = (last.close - entry_price) * sign
    return {
        "mfe_exit": mfe_exit,
        "mae_exit": mae_exit,
        "mfe_deadline": mfe_deadline,
        "alt_points": alt_exit_points,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    trades = load_trades(args.journal)
    print(f"{len(trades)} trades")

    rows = []
    bars_cache: dict[str, list[Bar]] = {}
    for trade in trades:
        session = trade["session"]
        if session not in bars_cache:
            fixture = args.fixtures / f"{session}.jsonl"
            bars_cache = {session: one_minute_bars(fixture)}  # one session at a time
        bars = bars_cache[session]

        direction = trade["direction"]
        long = direction == "LONG"
        sign = 1 if long else -1
        entry_price = Decimal(str(trade["entry_price"]))
        exit_price = Decimal(str(trade["exit_price"]))
        entry_time = datetime.fromisoformat(trade["entry_time"])
        exit_time = datetime.fromisoformat(trade["exit_time"])
        signal = trade["signal"] or {}
        stop = Decimal(str(signal["stop_price"])) if signal.get("stop_price") else None
        risk = (entry_price - stop) * sign if stop is not None else None

        ex = excursions(bars, direction, entry_price, stop, entry_time, exit_time)
        points = (exit_price - entry_price) * sign
        alt = ex["alt_points"]

        def r_mult(value: Decimal, risk: Decimal | None = risk) -> str | None:
            if not risk:
                return None
            return str((value / risk).quantize(Decimal("0.01")))

        rows.append(
            {
                "session": session,
                "year": session[:4],
                "month": session[:7],
                "dow": date.fromisoformat(session).strftime("%a"),
                "entry_hour_et": entry_time.astimezone(ET).hour,
                "direction": direction,
                "exit_reason": trade["exit_reason"],
                "quantity": trade["quantity"],
                "points": str(points),
                "net_dollars": str(points * POINT_VALUE - COMMISSION_RT),
                "risk_points": None if risk is None else str(risk),
                "realised_r": r_mult(points),
                "mfe_exit_r": r_mult(ex["mfe_exit"]),
                "mfe_deadline_r": r_mult(ex["mfe_deadline"]),
                "mae_exit_r": r_mult(ex["mae_exit"]),
                "alt_points": None if alt is None else str(alt),
                "alt_net_dollars": None if alt is None else str(alt * POINT_VALUE - COMMISSION_RT),
            }
        )

    args.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")

    # ---- aggregate views ---------------------------------------------------
    def bucket(rows: list[dict[str, Any]], key: str) -> None:
        groups: dict[str, list[Decimal]] = defaultdict(list)
        for row in rows:
            groups[str(row[key])].append(Decimal(row["net_dollars"]))
        print(f"\n-- by {key}:")
        for name in sorted(groups):
            values = groups[name]
            wins = sum(1 for v in values if v > 0)
            ev = sum(values) / len(values)
            print(
                f"  {name:<12} n={len(values):<5} net=${sum(values):>12,.2f} "
                f"EV=${ev:>8,.2f} win={wins / len(values):>5.0%}"
            )

    for key in ("year", "direction", "exit_reason", "entry_hour_et", "dow"):
        bucket(rows, key)

    with_r = [r for r in rows if r["mfe_deadline_r"] is not None]
    if with_r:
        for threshold in ("1", "2", "3"):
            reached = sum(
                1 for r in with_r if Decimal(r["mfe_deadline_r"]) >= Decimal(threshold)
            )
            share = reached / len(with_r)
            print(f"reached +{threshold}R before 15:55: {reached}/{len(with_r)} ({share:.0%})")

    alt_rows = [r for r in rows if r["alt_net_dollars"] is not None]
    real_total = sum(Decimal(r["net_dollars"]) for r in rows)
    alt_total = sum(Decimal(r["alt_net_dollars"]) for r in alt_rows)
    print(f"\nreal exits net:           ${real_total:>12,.2f}  ({len(rows)} trades)")
    print(f"initial-stop-only net:    ${alt_total:>12,.2f}  ({len(alt_rows)} trades)")


if __name__ == "__main__":
    main()
