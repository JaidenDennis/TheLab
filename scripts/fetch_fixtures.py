"""Pull NQ data from Databento and write replay fixtures, one file per session.

Three schemas, three purposes:

  trades     True tick fixtures. Each record keeps the aggressor side the
             exchange reported, so the OFI gate's "full" mode has the data
             it needs the day the engine learns to carry it. ~12 MB billable
             per RTH session; free inside the plan's 1-year L1 window.
  ohlcv-1m   Pseudo-tick fixtures: each 1m bar expands to open/high/low/
             close as four ticks (volume on the close tick). For THIS
             engine these are lossless -- the aggregator rebuilds the very
             same 1m bars, and every downstream component (tracker, SME,
             risk) is bar-driven -- at 1/60th the size of 1s data. The
             workhorse for deep history.
  ohlcv-1s   Same expansion at 1s granularity. Only worth the size if a
             future fill model goes sub-minute.

Sessions are ET calendar dates, matching the engine's SessionCalendar. The
UTC window is computed per-day from America/New_York, so DST is handled by
the timezone database rather than by anyone's memory.

  --window rth        09:30 -> 16:31 ET (the engine's session, plus the
                      one minute that closes the final bucket -- see
                      window_bounds)
  --window extended   00:00 -> 16:31 ET, adding the overnight portion the
                      exhaustion filter can see (midnight to open)

Requests are chunked (weekly for trades, monthly for bars) and split into
per-session files locally: one API call per day would be 1,400 calls for
five years of history.

Always run with --quote first: it prints the billable size and dollar cost
of the exact request set and pulls nothing.

Usage:
  uv run python scripts/fetch_fixtures.py --start 2021-01-04 --end 2026-08-12 \
      --schema ohlcv-1m --window extended --out var/fixtures/1m --quote
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db

ET = ZoneInfo("America/New_York")
TICK = Decimal("0.25")
PRICE_SCALE = Decimal(10) ** 9

# One minute past the engine's 16:30 cutoff, ON PURPOSE: ticks at/after
# 16:30 are what roll the 16:29-16:30 bucket closed, and that final bar is
# the one the session cutoff flatten fires on. End the data at 16:30 and the
# last bucket stays open forever, the flatten never happens, and every
# backtest ends silently holding a position it never books.
SESSION_END = time(16, 31)


def load_key(env_path: Path) -> str:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("NQ_DATABENTO_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if key:
                return key
    raise SystemExit(f"NQ_DATABENTO_API_KEY not found in {env_path}")


def window_bounds(session: date, window: str) -> tuple[datetime, datetime]:
    # rth15 starts 09:15 so trailing 15-minute flow windows are complete
    # from the first tradable bar.
    starts = {"rth": time(9, 30), "rth15": time(9, 15), "extended": time(0, 0)}
    start_local = starts[window]
    start = datetime.combine(session, start_local, tzinfo=ET).astimezone(timezone.utc)
    end = datetime.combine(session, SESSION_END, tzinfo=ET).astimezone(timezone.utc)
    return start, end


def sessions_between(start: date, end: date) -> list[date]:
    """Weekdays only. Exchange holidays return no data and produce no file."""
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def chunks(days: list[date], size_days: int) -> list[list[date]]:
    grouped: list[list[date]] = []
    for session in days:
        if grouped and (session - grouped[-1][0]).days < size_days:
            grouped[-1].append(session)
        else:
            grouped.append([session])
    return grouped


def px(raw: int) -> str:
    """Databento fixed-precision (1e-9) to a decimal string on the NQ grid."""
    return str((Decimal(raw) / PRICE_SCALE).quantize(TICK))


def iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat(
        timespec="microseconds"
    )


def trade_lines(record: object) -> list[tuple[int, str]]:
    if record.size <= 0:  # type: ignore[attr-defined]
        return []
    ns = record.ts_event  # type: ignore[attr-defined]
    line = json.dumps(
        {
            "ts": iso(ns),
            "price": px(record.price),  # type: ignore[attr-defined]
            "size": record.size,  # type: ignore[attr-defined]
            # Aggressor side as the exchange reported it (A/B/N). ReplayFeed
            # ignores unknown keys today; the full-OFI data layer will not.
            "side": str(getattr(record.side, "value", record.side)),  # type: ignore[attr-defined]
        }
    )
    return [(ns, line)]


def ohlcv_lines(record: object) -> list[tuple[int, str]]:
    """Four pseudo-ticks per bar, ordered open -> nearer extreme -> farther
    extreme -> close inside the bar's first second so downstream bucketing
    keeps them in order. Volume rides on the close tick, so rebuilt bars
    carry the true bar volume."""
    o = record.open  # type: ignore[attr-defined]
    h = record.high  # type: ignore[attr-defined]
    lo = record.low  # type: ignore[attr-defined]
    c = record.close  # type: ignore[attr-defined]
    volume = record.volume  # type: ignore[attr-defined]
    base = record.ts_event  # type: ignore[attr-defined]
    extremes = (lo, h) if c >= o else (h, lo)
    out = []
    for offset, (raw, size) in enumerate(
        [(o, 0), (extremes[0], 0), (extremes[1], 0), (c, volume)]
    ):
        ns = base + offset * 250_000_000
        out.append((ns, json.dumps({"ts": iso(ns), "price": px(raw), "size": int(size)})))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--schema", choices=["trades", "ohlcv-1m", "ohlcv-1s"], default="trades")
    parser.add_argument("--window", choices=["rth", "rth15", "extended"], default="rth")
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=None,
        help="request-chunk size in days; default 7 for trades, 31 for bars. Use 1 "
        "for billed pulls: a chunk spanning nights and weekends bills the span, "
        "not the sessions kept from it",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--symbol", default="NQ.v.0", help="volume-rolled continuous front month")
    parser.add_argument("--quote", action="store_true", help="print cost and size, pull nothing")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args()

    client = db.Historical(load_key(args.env))
    days = sessions_between(args.start, args.end)
    if not days:
        raise SystemExit("no weekdays in range")
    chunk_days = args.chunk_days or (7 if args.schema == "trades" else 31)
    grouped = chunks(days, chunk_days)

    if args.quote:
        total_cost = 0.0
        total_bytes = 0
        for group in grouped:
            start, _ = window_bounds(group[0], args.window)
            _, end = window_bounds(group[-1], args.window)
            kwargs = dict(
                dataset="GLBX.MDP3",
                symbols=[args.symbol],
                stype_in="continuous",
                schema=args.schema,
                start=start,
                end=end,
            )
            total_cost += client.metadata.get_cost(**kwargs)
            total_bytes += client.metadata.get_billable_size(**kwargs)
        print(
            f"{len(days)} session(s) in {len(grouped)} request(s), schema {args.schema}, "
            f"window {args.window}: {total_bytes / 1e6:,.1f} MB billable, ${total_cost:,.2f}"
        )
        print("(chunked quotes include overnight/weekend spans inside each chunk,")
        print(" so treat this as an upper bound; the pull itself is filtered per session)")
        return

    args.out.mkdir(parents=True, exist_ok=True)
    convert = trade_lines if args.schema == "trades" else ohlcv_lines
    written = 0
    for group in grouped:
        pending = [d for d in group if not (args.out / f"{d.isoformat()}.jsonl").exists()]
        if not pending:
            continue
        start, _ = window_bounds(group[0], args.window)
        _, end = window_bounds(group[-1], args.window)
        store = client.timeseries.get_range(
            dataset="GLBX.MDP3",
            symbols=[args.symbol],
            stype_in="continuous",
            schema=args.schema,
            start=start,
            end=end,
        )

        # Bucket into sessions locally, filtering each record to its own
        # session's window -- the API request spans nights and weekends.
        wanted = {d: window_bounds(d, args.window) for d in pending}
        buckets: dict[date, list[str]] = {d: [] for d in pending}
        for record in store:
            for ns, line in convert(record):
                stamp = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
                session = stamp.astimezone(ET).date()
                bounds = wanted.get(session)
                if bounds is None or not (bounds[0] <= stamp < bounds[1]):
                    continue
                buckets[session].append(line)

        for session in pending:
            lines = buckets[session]
            if not lines:
                continue  # holiday
            out_path = args.out / f"{session.isoformat()}.jsonl"
            out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            written += 1
        print(f"{group[0]}..{group[-1]}: wrote {sum(1 for d in pending if buckets[d])} file(s)")

    print(f"wrote {written} fixture(s) to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
