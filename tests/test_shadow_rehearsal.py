"""The shadow harness, end to end, exactly as run_shadow wires it.

This is the test the first live session was missing: a full synthetic
trading day (overnight ticks included) through the COMPLETE live path --
ReplayFeed tick tap -> FlowEngine -> shared decision book (EMPTY at
strategy construction, the live-only condition every backtest skipped) ->
TickFlowRegime fc_t13 -> production engine -> dryrun executor -> journal.
If any seam of that chain severs, this fails the way the first live
session failed: silently flat all day.

The synthetic day: overnight drift (must be ignored), a flat opening
half-hour, then a sustained one-sided buy burst from 09:50 that must
clear Q70 flow and z_vol volume, enter LONG, and exit on the 13-bar
clock. Deterministic; no network, no artifacts.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nq_agent.clock import SimClock
from nq_agent.feed.replay import ReplayFeed
from nq_agent.flow import FlowEngine
from nq_agent.main import FeedBinding, run_from_config
from nq_agent.strategy.tfr import TickFlowRegime

MIDNIGHT = datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)  # 00:00 ET (EDT)


def synthetic_day(path: Path) -> None:
    lines = []

    def tick(minutes: float, price: str, size: int, side: str) -> None:
        stamp = (MIDNIGHT + timedelta(minutes=minutes)).isoformat()
        lines.append(json.dumps({"ts": stamp, "price": price, "size": size, "side": side}))

    # Overnight: sparse two-sided ticks, 01:00-09:29 ET. Must not trade.
    for m in range(60, 565, 5):
        tick(m + 0.5, "20000.00", 1, "B" if m % 10 else "A")
    # RTH open 09:30-09:50: flat, two-sided, modest volume.
    for m in range(570, 590):
        tick(m + 0.3, "20000.00", 2, "B")
        tick(m + 0.6, "20000.25", 2, "A")
    # 09:50-10:00: heavy one-sided buy burst -- flow and volume both spike.
    price = 20000.0
    for m in range(590, 600):
        price += 1.0
        tick(m + 0.3, f"{price:.2f}", 8, "B")
        tick(m + 0.6, f"{price + 0.5:.2f}", 8, "B")
    # 10:00-16:00: drift with steady two-sided tape so bars keep closing.
    for m in range(600, 960):
        tick(m + 0.5, f"{price + (m - 600) * 0.01:.2f}", 2, "B" if m % 2 else "A")
    # One tick past 16:31 ET so the final RTH buckets roll closed.
    tick(992.5, f"{price:.2f}", 1, "B")
    path.write_text("\n".join(lines) + "\n")


CALIBRATION: dict[str, Any] = {
    # Q70 low enough that the burst's F1 clears it; vol stats such that the
    # burst's 5m volume z-scores far above 0.5 while the flat tape does not.
    "q_f1": {"55": 0.05, "60": 0.06, "65": 0.07, "70": 0.08, "75": 0.1, "80": 0.15, "85": 0.2},
    "vol_mean": 40.0,
    "vol_sd": 20.0,
    "size_cut": 5,
}


def test_full_day_through_the_exact_live_wiring(tmp_path: Path) -> None:
    fixture = tmp_path / "2026-07-15.jsonl"
    synthetic_day(fixture)
    config = tmp_path / "shadow.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m, 5m]\n"
        "contract:\n  point_value: 2\n  commission_per_round_turn: 1\n"
        "risk:\n  max_trades_per_day: 10\n  duplicate_window_seconds: 0\n"
        "executors:\n  - name: shadow_paper\n    type: dryrun\n    enabled: true\n    accounts: [shadow]\n"
    )

    # THE live-only condition: the book is EMPTY when the strategy is built.
    book: dict[str, Any] = {}
    engine = FlowEngine(CALIBRATION, book)
    clock = SimClock(ReplayFeed(fixture, "NQ").first_tick_time())
    feed = ReplayFeed(fixture, "NQ", clock=clock, tick_tap=engine.on_tick)
    binding = FeedBinding(feed=feed, clock=clock, anchor=clock.now(), resume_latest=True)
    strategy = TickFlowRegime(decisions=book, exit_mode="t13", regime_required=False)

    asyncio.run(
        run_from_config(config, None, "tfr", None, strategy_override=strategy, binding=binding)
    )

    journal = tmp_path / "journal" / "2026-07-15.jsonl"
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    kinds = [e["event"] for e in events]

    assert book["2026-07-15"]["bars"], "the flow engine never emitted decision bars"
    opened = [e for e in events if e["event"] == "position_opened"]
    assert opened, (
        "the burst never produced a trade -- the live wiring is severed somewhere "
        f"(journal: {kinds})"
    )
    closed = [e for e in events if e["event"] == "position_closed"]
    assert closed and closed[0]["direction"] == "LONG"
    assert closed[0]["exit_reason"] == "FLATTEN"  # the 13-bar clock, via engine flatten

    # And the day must END flat with the session closed properly.
    assert "session_end" in kinds
