"""The shared flow layer: one implementation for research and live.

These tests pin the mechanics the drift audit depends on: boundary-driven
emission (a decision bar exists the moment a tick crosses its 5m boundary,
before the bar could reach a strategy), session reset, tick-rule fallback,
and the tap ordering through ReplayFeed.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from nq_agent.feed.replay import ReplayFeed
from nq_agent.flow import ET, FlowEngine, MinuteFlowAggregator, flow_over, minute_index
from nq_agent.models import Tick

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 ET (EDT)


def tick(minutes_in: float, price: str, size: int = 2, side: str | None = "B") -> Tick:
    return Tick(
        symbol="NQ",
        ts=OPEN + timedelta(minutes=minutes_in),
        price=Decimal(price),
        size=size,
        side=side,
    )


CAL = {"q_f1": {"70": 0.15}, "vol_mean": 10.0, "vol_sd": 5.0, "size_cut": 5}


def test_minute_index_maps_rth() -> None:
    assert minute_index(tick(0.5, "20000").ts.astimezone(ET)) == 1
    assert minute_index(tick(389.5, "20000").ts.astimezone(ET)) == 390


def test_aggregator_signs_volume_by_aggressor() -> None:
    agg = MinuteFlowAggregator()
    agg.on_tick(tick(0.2, "20000", size=3, side="B"))
    agg.on_tick(tick(0.4, "20001", size=2, side="A"))

    bucket = agg.minutes[1]
    assert bucket["buy"] == 3 and bucket["sell"] == 2 and bucket["vol"] == 5


def test_unknown_side_falls_back_to_tick_rule() -> None:
    agg = MinuteFlowAggregator()
    agg.on_tick(tick(0.1, "20000", side="B"))
    agg.on_tick(tick(0.2, "20001", side=None))  # uptick -> buy by rule

    assert agg.minutes[1]["buy"] == 4
    assert agg.qa()["unknown_share"] == 0.5


def test_decision_bar_exists_once_the_boundary_is_crossed() -> None:
    book: dict[str, dict] = {}
    engine = FlowEngine(CAL, book)
    for m in range(5):  # ticks in minutes 1..5 (09:30:30 .. 09:34:30)
        engine.on_tick(tick(m + 0.5, "20000", size=4, side="B"))
    session = "2026-07-15"
    assert book[session]["bars"] == {}  # boundary not crossed yet

    engine.on_tick(tick(5.5, "20001", side="B"))  # first tick of minute 6

    record = book[session]["bars"]["5"]
    assert record["f1_5"] == 1.0  # all buy volume
    assert record["close"] == 20000.0
    assert record["z_vol"] == 2.0  # vol 20 vs mean 10 sd 5


def test_session_change_resets_aggregates_and_emission() -> None:
    """Day-2 records must be built from day-2 ticks only: without the reset,
    day-1 minutes bleed into day-2 buckets and the emission cursor skips
    day-2's first bars entirely."""
    book: dict[str, dict] = {}
    engine = FlowEngine(CAL, book)
    for m in range(5):
        engine.on_tick(tick(m + 0.5, "20000", size=4, side="B"))
    engine.on_tick(tick(5.5, "20000", side="B"))  # emits day-1 bar 5, f1=+1

    def day2(minutes_in: float, side: str) -> Tick:
        return Tick(
            symbol="NQ",
            ts=OPEN + timedelta(days=1, minutes=minutes_in),
            price=Decimal("20000"),
            size=4,
            side=side,
        )

    for m in range(5):
        engine.on_tick(day2(m + 0.5, "A"))  # all sells
    engine.on_tick(day2(5.5, "A"))

    assert book["2026-07-15"]["bars"]["5"]["f1_5"] == 1.0
    day2_bar = book["2026-07-16"]["bars"].get("5")
    assert day2_bar is not None, "day-2 bar 5 was never emitted (cursor not reset)"
    assert day2_bar["f1_5"] == -1.0, "day-1 buys bled into day-2 aggregates"


def test_finish_session_emits_the_trailing_bars() -> None:
    book: dict[str, dict] = {}
    engine = FlowEngine(CAL, book)
    for m in range(388, 390):  # minutes 389, 390 (ticks after 15:58)
        engine.on_tick(tick(m + 0.5, "20000", side="B"))
    assert "390" not in book["2026-07-15"]["bars"]

    engine.finish_session()

    assert "390" in book["2026-07-15"]["bars"]


def test_replay_tap_runs_before_the_bar_reaches_a_consumer(tmp_path: Path) -> None:
    """The ordering contract the whole shadow design rests on: when a closed
    5m bar comes out of the feed, its decision record already exists."""
    lines = []
    for m in range(6):
        stamp = (OPEN + timedelta(minutes=m, seconds=30)).isoformat()
        lines.append(json.dumps({"ts": stamp, "price": "20000.00", "size": 2, "side": "B"}))
    fixture = tmp_path / "2026-07-15.jsonl"
    fixture.write_text("\n".join(lines) + "\n")

    book: dict[str, dict] = {}
    engine = FlowEngine(CAL, book)
    feed = ReplayFeed(fixture, "NQ", tick_tap=engine.on_tick)

    import asyncio

    async def collect() -> list:
        return [bar async for bar in feed.stream("NQ", ["5m"])]

    bars = asyncio.run(collect())
    five = [b for b in bars if b.timeframe == "5m"]
    assert five, "the fixture must close one 5m bar"
    index = str(minute_index(five[0].close_time.astimezone(ZoneInfo("America/New_York"))) - 1)
    assert index in book["2026-07-15"]["bars"], (
        "the decision record must exist by the time the closed bar is yielded"
    )


def test_flow_over_spans_missing_minutes() -> None:
    minutes = {1: {"buy": 3, "sell": 1, "vol": 4}, 3: {"buy": 0, "sell": 2, "vol": 2}}
    assert flow_over(minutes, 3, 5) == (3 - 3) / 6
