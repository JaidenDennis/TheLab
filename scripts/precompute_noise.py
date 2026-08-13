"""Precompute NAIM's noise curves: sigma(t) per session, per lookback.

For each session d and each RTH minute t (close times 09:31..16:00 ET),
sigma(t) is the mean over the trailing L COMPLETED sessions of
abs(P_t - Open) / Open. Zero lookahead: session d's curve uses sessions
strictly before d, so it is knowable before d's open. Sessions with fewer
than L predecessors get no curve, which is the strategy's warmup.

Also emits each session's RTH open and prior close, so the strategy can
compute the gap-adjusted anchors (max/min of open and prior close) without
re-reading fixtures.

Output: one JSON per lookback -- var/noise/L{n}.json:
  {"2021-05-10": {"open": "13400.25", "prev_close": "13390.00",
                  "sigma": {"1": 0.00042, ..., "390": 0.0031}}, ...}

Usage:
  uv run python scripts/precompute_noise.py --fixtures var/fixtures/1m \
      --out var/noise --lookbacks 14 30 60 90 120
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from datetime import time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from nq_agent.feed.aggregator import BarAggregator
from nq_agent.feed.replay import ReplayFeed

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


def session_profile(fixture: Path) -> tuple[str | None, str | None, dict[int, float]]:
    """(rth_open, rth_close, {minute_index: abs_move_fraction}) for one session.

    Minute index 1 == the bar closing 09:31 ET; 390 == the 16:00 close.
    """
    aggregator = BarAggregator("NQ", ["1m"])
    bars = []
    for tick in ReplayFeed(fixture, "NQ")._ticks():
        bars.extend(aggregator.add_tick(tick))
    bars.extend(b for b in aggregator.flush() if b.closed)

    open_price: Decimal | None = None
    last_close: Decimal | None = None
    moves: dict[int, float] = {}
    for bar in bars:
        et = bar.close_time.astimezone(ET)
        if not (RTH_OPEN < et.time() <= RTH_CLOSE):
            continue
        if open_price is None:
            open_price = bar.open
        last_close = bar.close
        index = (et.hour - 9) * 60 + et.minute - 30
        moves[index] = float(abs(bar.close - open_price) / open_price)
    return (
        None if open_price is None else str(open_price),
        None if last_close is None else str(last_close),
        moves,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lookbacks", type=int, nargs="+", default=[90])
    args = parser.parse_args()

    fixtures = sorted(args.fixtures.glob("*.jsonl"))
    print(f"{len(fixtures)} sessions")
    args.out.mkdir(parents=True, exist_ok=True)

    profiles: list[tuple[str, str, str, dict[int, float]]] = []
    for fixture in fixtures:
        open_price, prev_close, moves = session_profile(fixture)
        if open_price is None or not moves:
            continue
        profiles.append((fixture.stem, open_price, prev_close or open_price, moves))

    for lookback in args.lookbacks:
        curves: dict[str, dict[str, object]] = {}
        window: deque[dict[int, float]] = deque(maxlen=lookback)
        prev_session_close: str | None = None
        for session, open_price, session_close, moves in profiles:
            if len(window) == lookback and prev_session_close is not None:
                # sigma per minute over the trailing L sessions; a minute
                # missing from some sessions (half days) averages what exists.
                sigma: dict[str, float] = {}
                for index in range(1, 391):
                    values = [m[index] for m in window if index in m]
                    if len(values) >= lookback // 2:
                        sigma[str(index)] = sum(values) / len(values)
                curves[session] = {
                    "open": open_price,
                    "prev_close": prev_session_close,
                    "sigma": sigma,
                }
            window.append(moves)
            prev_session_close = session_close

        out_path = args.out / f"L{lookback}.json"
        out_path.write_text(json.dumps(curves), encoding="utf-8")
        print(f"L={lookback}: {len(curves)} sessions with curves -> {out_path}")


if __name__ == "__main__":
    main()
