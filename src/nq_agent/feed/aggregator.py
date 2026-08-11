from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from nq_agent.models import TIMEFRAME_SECONDS, Bar, Tick


@dataclass
class _Builder:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def update(self, tick: Tick) -> None:
        self.high = max(self.high, tick.price)
        self.low = min(self.low, tick.price)
        self.close = tick.price
        self.volume += tick.size


def _bucket_start(ts: datetime, seconds: int) -> datetime:
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


class BarAggregator:
    """Builds closed bars from ticks, one bucket per timeframe.

    Every timeframe is built directly from ticks rather than rolled up from
    the base timeframe. The result is identical when no bucket is empty, and
    strictly more correct when one is — a 5m bar still forms even if a
    constituent minute saw zero trades.
    """

    def __init__(self, symbol: str, timeframes: list[str]) -> None:
        duplicates = sorted({tf for tf in timeframes if timeframes.count(tf) > 1})
        if duplicates:
            # self._builders is keyed by timeframe, so a repeat would share one
            # bucket and take tick.size twice: silently doubled volume, correct
            # OHLC, no error. Reject it rather than dedupe, so a config typo is
            # visible instead of quietly tolerated.
            raise ValueError(f"duplicate timeframes: {duplicates}")
        for timeframe in timeframes:
            if timeframe not in TIMEFRAME_SECONDS:
                raise ValueError(f"unsupported timeframe: {timeframe}")
        self._symbol = symbol
        self._timeframes = sorted(timeframes, key=lambda tf: TIMEFRAME_SECONDS[tf])
        self._builders: dict[str, _Builder | None] = dict.fromkeys(self._timeframes)
        self._last_ts: datetime | None = None

    def _finish(self, timeframe: str, builder: _Builder, *, closed: bool = True) -> Bar:
        return Bar(
            symbol=self._symbol,
            timeframe=timeframe,
            open_time=builder.open_time,
            open=builder.open,
            high=builder.high,
            low=builder.low,
            close=builder.close,
            volume=builder.volume,
            closed=closed,
        )

    def _ordered(self, bars: list[Bar]) -> list[Bar]:
        """Sort by close time, shorter timeframe first on a tie.

        The duration tie-break is load-bearing even though callers happen to
        build `bars` in ascending-duration order already: a 5m reaching the
        strategy before the 1m closing on its own boundary is a lookahead bug.
        Tested directly in test_ordered_puts_the_shorter_timeframe_first, which
        passes pre-shuffled input, because no public-API test can falsify it.
        """
        return sorted(bars, key=lambda b: (b.close_time, TIMEFRAME_SECONDS[b.timeframe]))

    def add_tick(self, tick: Tick) -> list[Bar]:
        if self._last_ts is not None and tick.ts < self._last_ts:
            raise ValueError(f"tick out of order: {tick.ts} < {self._last_ts}")
        self._last_ts = tick.ts

        closed: list[Bar] = []
        for timeframe in self._timeframes:
            seconds = TIMEFRAME_SECONDS[timeframe]
            start = _bucket_start(tick.ts, seconds)
            builder = self._builders[timeframe]

            if builder is not None and start > builder.open_time:
                closed.append(self._finish(timeframe, builder))
                builder = None

            if builder is None:
                self._builders[timeframe] = _Builder(
                    open_time=start,
                    open=tick.price,
                    high=tick.price,
                    low=tick.price,
                    close=tick.price,
                    volume=tick.size,
                )
            else:
                builder.update(tick)

        return self._ordered(closed)

    def flush(self) -> list[Bar]:
        """Force-close every open bucket, whether or not its window has
        actually elapsed.

        A bucket flush() finds open is, by construction, one add_tick never
        rolled over -- no tick at or past its close_time ever arrived, or
        add_tick would have closed it already. That means the bar this
        produces is only genuinely closed if the caller has other grounds to
        believe no more ticks are coming and time has actually moved past
        close_time; flush() itself only knows the tick stream stopped, not
        that the clock did. So each bucket here is marked `closed` only when
        its close_time is at or before the last tick actually seen -- for a
        single, uninterrupted tick stream that is never true (a bucket still
        open after the last tick necessarily has a close_time later than
        that tick), so in practice flush() marks everything it emits as
        partial. That is deliberate, not dead code: it is what stops "bars
        are emitted only when closed" from being silently violated by a
        stream that just happens to end mid-bucket. Callers that explicitly
        want the partial (this method) can still have it; callers bound by
        the "closed bars only" contract (DataFeed.stream, DataFeed.get_bars)
        must filter on `.closed` before yielding what this returns.
        """
        closed: list[Bar] = []
        for timeframe in self._timeframes:
            builder = self._builders[timeframe]
            if builder is not None:
                close_time = builder.open_time + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
                is_closed = self._last_ts is not None and close_time <= self._last_ts
                closed.append(self._finish(timeframe, builder, closed=is_closed))
                self._builders[timeframe] = None
        return self._ordered(closed)
