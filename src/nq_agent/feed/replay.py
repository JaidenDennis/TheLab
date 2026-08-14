from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SimClock
from nq_agent.feed.aggregator import BarAggregator
from nq_agent.feed.base import DataFeed
from nq_agent.models import Bar, Tick


class ReplayFeed(DataFeed):
    """Deterministic playback of a JSONL tick fixture.

    No network, no sleeping. When a SimClock is supplied it is advanced to each
    bar's close time immediately before that bar is yielded, so downstream
    components observe exactly the time the bar represents.
    """

    def __init__(
        self,
        fixture_path: Path,
        symbol: str,
        clock: SimClock | None = None,
        tick_tap: Callable[[Tick], None] | None = None,
    ) -> None:
        self._path = fixture_path
        self._symbol = symbol
        self._clock = clock
        # Same contract as DatabentoFeed's tap: every tick, before
        # aggregation. Lets the shadow harness rehearse on fixtures through
        # the identical code path it will run live.
        self._tick_tap = tick_tap

    def _ticks(self) -> Iterator[Tick]:
        with self._path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                record = json.loads(line)
                yield Tick(
                    symbol=self._symbol,
                    ts=datetime.fromisoformat(record["ts"]),
                    price=Decimal(record["price"]),
                    size=int(record["size"]),
                    side=record.get("side"),
                )

    def first_tick_time(self) -> datetime:
        """First timestamp in the fixture, read without parsing the whole file."""
        for tick in self._ticks():
            return tick.ts
        raise ValueError(f"replay fixture is empty: {self._path}")

    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        aggregator = BarAggregator(symbol, [timeframe])
        collected: list[Bar] = []
        for tick in self._ticks():
            collected.extend(aggregator.add_tick(tick))
        # get_bars is documented ("Historical closed bars") the same as
        # stream(): flush()'s trailing bucket may be genuinely partial (see
        # its docstring), and this contract does not get to hand that out
        # just because it is the last one in the requested window.
        collected.extend(bar for bar in aggregator.flush() if bar.closed)
        return [bar for bar in collected if start <= bar.open_time < end]

    async def stream(
        self, symbol: str, timeframes: list[str], resume_from: datetime | None = None
    ) -> AsyncIterator[Bar]:
        # resume_from is accepted for interface parity. A fixture always replays
        # in full, so the history a live feed would have to backfill is already here.
        aggregator = BarAggregator(symbol, timeframes)
        for tick in self._ticks():
            if self._tick_tap is not None:
                self._tick_tap(tick)
            for bar in aggregator.add_tick(tick):
                if self._clock is not None:
                    self._clock.advance_to(bar.close_time)
                yield bar
        for bar in aggregator.flush():
            # DataFeed promises closed bars only (see its docstring). The
            # fixture ending mid-bucket does not get an exception to that --
            # flush() marks exactly that bucket closed=False, so it is
            # dropped here rather than yielded as if it were a real bar.
            if not bar.closed:
                continue
            if self._clock is not None:
                self._clock.advance_to(bar.close_time)
            yield bar

    async def close(self) -> None:
        return None
