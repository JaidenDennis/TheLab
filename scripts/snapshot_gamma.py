"""Daily pre-open gamma snapshot for GFM Track B (and historical backfill).

Per session date D:
  1. QQQ chain definitions for D           (strikes, expiries, call/put)
  2. The 06:30 ET open-interest batch on D (positions as of D-1 close)
  3. Close quotes from D-1's last 5 RTH minutes -> per-contract mid
  4. compute_gex under the standard convention, plus the two pre-declared
     robustness variants (inverse dealer sign; 0DTE excluded)
  5. classify_day against the trailing 120-session NetGEX history
  6. Archive the PARSED chain (sufficient to reproduce every number) and
     append the regime line to var/gamma/regimes.jsonl

The regime file is annotation infrastructure: nothing in the trading
engine reads it. Track A/B joins happen offline by date.

Usage:
  uv run python scripts/snapshot_gamma.py                    # today, ~$0.71
  uv run python scripts/snapshot_gamma.py --date 2026-08-01
  uv run python scripts/snapshot_gamma.py --backfill 2025-08-14 2026-08-13 --quote
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db

from nq_agent.gex import ChainEntry, classify_day, compute_gex

ET = ZoneInfo("America/New_York")
GAMMA_DIR = Path("var/gamma")
REGIMES = GAMMA_DIR / "regimes.jsonl"
TRAILING = 120


def load_key() -> str:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("NQ_DATABENTO_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("NQ_DATABENTO_API_KEY not found in .env")


def prior_weekday(day: date) -> date:
    prior = day - timedelta(days=1)
    while prior.weekday() >= 5:
        prior -= timedelta(days=1)
    return prior


def windows(day: date) -> dict[str, tuple[datetime, datetime]]:
    prior = prior_weekday(day)
    quote_start = datetime.combine(prior, time(15, 55), tzinfo=ET).astimezone(timezone.utc)
    quote_end = datetime.combine(prior, time(16, 0), tzinfo=ET).astimezone(timezone.utc)
    oi_start = datetime.combine(day, time(5, 30), tzinfo=ET).astimezone(timezone.utc)
    oi_end = datetime.combine(day, time(8, 0), tzinfo=ET).astimezone(timezone.utc)
    return {
        # Definitions publish pre-open; end mid-day so a same-day snapshot
        # never outruns the historical API's availability lag.
        "definition": (
            datetime.combine(day, time(0, 0), tzinfo=timezone.utc),
            # 13:00Z: before OPRA's live-license recency gate (13:30Z on a
            # same-day pull) and after the pre-open definition batch.
            datetime.combine(day, time(13, 0), tzinfo=timezone.utc),
        ),
        "statistics": (oi_start, oi_end),
        "cbbo-1m": (quote_start, quote_end),
    }


def quote_cost(client: db.Historical, day: date) -> tuple[float, float]:
    total_cost = 0.0
    total_bytes = 0.0
    for schema, (start, end) in windows(day).items():
        kwargs = dict(
            dataset="OPRA.PILLAR", symbols=["QQQ.OPT"], stype_in="parent",
            schema=schema, start=start, end=end,
        )
        total_cost += client.metadata.get_cost(**kwargs)
        total_bytes += client.metadata.get_billable_size(**kwargs)
    return total_cost, total_bytes


def snapshot(client: db.Historical, day: date) -> dict | None:
    win = windows(day)

    def pull(schema: str):
        start, end = win[schema]
        return client.timeseries.get_range(
            dataset="OPRA.PILLAR", symbols=["QQQ.OPT"], stype_in="parent",
            schema=schema, start=start, end=end,
        )

    # 1. definitions: instrument_id -> contract terms
    terms: dict[int, tuple[float, float, bool]] = {}
    for rec in pull("definition"):
        cls = str(getattr(rec.instrument_class, "value", rec.instrument_class))
        if cls not in ("C", "P"):
            continue
        expiry = datetime.fromtimestamp(rec.expiration / 1e9, tz=timezone.utc)
        expiry_days = max(0.05, (expiry.date() - day).days + 0.65)  # ~16:00 ET expiry
        terms[rec.instrument_id] = (rec.strike_price / 1e9, expiry_days, cls == "C")

    # 2. the OI batch
    oi: dict[int, int] = {}
    for rec in pull("statistics"):
        if int(rec.stat_type) == 9 and rec.quantity is not None:
            oi[rec.instrument_id] = int(rec.quantity)

    # 3. last close mid per contract
    mids: dict[int, float] = {}
    for rec in pull("cbbo-1m"):
        bid = getattr(rec.levels[0], "bid_px", None)
        ask = getattr(rec.levels[0], "ask_px", None)
        if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
            continue
        mids[rec.instrument_id] = (bid + ask) / 2 / 1e9

    chain = [
        ChainEntry(
            strike=strike, expiry_days=expiry_days, is_call=is_call,
            open_interest=oi.get(iid, 0), mid=mids.get(iid, 0.0),
        )
        for iid, (strike, expiry_days, is_call) in terms.items()
        if oi.get(iid, 0) > 0 and mids.get(iid, 0.0) > 0
    ]
    if len(chain) < 100:
        return None

    result = compute_gex(chain)
    if result is None:
        return None
    inverse = compute_gex(chain, dealer_sign_calls=-1)
    no_0dte = compute_gex(chain, min_expiry_days=1.0)

    trailing = [
        json.loads(line)["net_gex"]
        for line in REGIMES.read_text().splitlines()
        if json.loads(line)["date"] < day.isoformat()
    ][-TRAILING:] if REGIMES.exists() else []
    regime = classify_day(result.net_gex, result.zero_flip, result.spot, trailing)

    archive = GAMMA_DIR / "raw" / f"{day.isoformat()}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(
        json.dumps(
            {
                "date": day.isoformat(),
                "chain": [
                    [e.strike, e.expiry_days, e.is_call, e.open_interest, e.mid]
                    for e in chain
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "date": day.isoformat(),
        "regime": regime,
        "net_gex": result.net_gex,
        "net_gex_inverse": inverse.net_gex if inverse else None,
        "net_gex_no0dte": no_0dte.net_gex if no_0dte else None,
        "zero_flip": result.zero_flip,
        "spot": result.spot,
        "contracts_used": result.contracts_used,
        "contracts_dropped": result.contracts_dropped,
        "trailing_n": len(trailing),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--backfill", nargs=2, type=date.fromisoformat, default=None)
    parser.add_argument("--quote", action="store_true")
    args = parser.parse_args()

    client = db.Historical(load_key())
    if args.backfill:
        start, end = args.backfill
        days = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                days.append(current)
            current += timedelta(days=1)
    else:
        days = [args.date or datetime.now(ET).date()]

    if args.quote:
        total = 0.0
        failures = []
        for day in days:
            try:
                cost, _ = quote_cost(client, day)
                total += cost
            except Exception as exc:  # noqa: BLE001 - one bad date must not kill the quote
                failures.append((day, f"{type(exc).__name__}: {str(exc)[:80]}"))
        print(f"{len(days) - len(failures)} session(s) quotable: ${total:,.2f}")
        for day, err in failures[:5]:
            print(f"  unquotable {day}: {err}")
        if len(failures) > 5:
            print(f"  ... and {len(failures) - 5} more")
        return

    GAMMA_DIR.mkdir(parents=True, exist_ok=True)
    existing = (
        {json.loads(line)["date"] for line in REGIMES.read_text().splitlines()}
        if REGIMES.exists()
        else set()
    )
    for day in days:
        if day.isoformat() in existing:
            print(f"{day}: already tagged, skipping")
            continue
        try:
            row = snapshot(client, day)
        except Exception as exc:  # noqa: BLE001 - keep the backfill moving
            print(f"{day}: FAILED {type(exc).__name__}: {str(exc)[:100]}")
            continue
        if row is None:
            print(f"{day}: no usable chain (holiday?), skipped")
            continue
        with REGIMES.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        print(
            f"{day}: {row['regime']}  net_gex={row['net_gex']:,.0f} "
            f"flip={row['zero_flip'] and round(row['zero_flip'], 2)} "
            f"spot={row['spot']:.2f} ({row['contracts_used']} contracts)"
        )


if __name__ == "__main__":
    main()
