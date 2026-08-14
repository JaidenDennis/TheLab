"""Databento market data, adapted to the DataFeed ABC.

VERIFICATION STATUS. The SDK surface used here was checked against the
installed databento 0.83.0 -- signatures, enum members, record field names and
FIXED_PRICE_SCALE are all real, not guessed. What has NOT been exercised is a
live connection: that needs a funded API key, and no request has ever been
sent from this code. Treat the shapes as verified and the behaviour as
untested until it has run a session in shadow mode.

Design notes:

- Trades, not the provider's own OHLCV. The existing BarAggregator already
  builds bars from ticks and is the component the whole test suite pins, so
  taking bars from a second source would mean two bar definitions that agree
  until the day they do not. It also means replay fixtures and live data go
  through identical code.

- Continuous symbology (`NQ.c.0`) rather than a dated contract. NQ rolls
  quarterly; a hardcoded `NQZ6` silently stops producing data after
  expiry, which looks exactly like a quiet market. `SType.CONTINUOUS` makes
  the front month the provider's problem. `.c.0` is volume-based roll --
  which means the roll happens mid-session, and a position open across it is
  in the old contract while new bars describe the new one. Do not hold
  through a roll.

- Reconnection is deliberately NOT handled here. The SDK has its own
  reconnect policy, and it is left at NONE: ReconnectingFeed already owns
  that concern, is provider-agnostic and is tested. Two competing reconnect
  loops is worse than one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal

from nq_agent.feed.aggregator import BarAggregator
from nq_agent.feed.base import DataFeed
from nq_agent.models import Bar, Tick

logger = logging.getLogger(__name__)

# Databento fixed-point prices are integers scaled by 1e9 (verified against
# databento.FIXED_PRICE_SCALE). Decimal, never float: a float divide here
# would put rounding error into every price before it reaches a signal.
PRICE_SCALE = Decimal("1000000000")

DEFAULT_DATASET = "GLBX.MDP3"  # CME Globex, where NQ trades


def _price(raw: int) -> Decimal:
    return Decimal(raw) / PRICE_SCALE


def _timestamp(ts_event: int) -> datetime:
    """ts_event is UTC nanoseconds since the epoch."""
    return datetime.fromtimestamp(ts_event / 1_000_000_000, tz=timezone.utc)


class DatabentoFeed(DataFeed):
    def __init__(
        self,
        api_key: str,
        symbol: str = "NQ.c.0",
        dataset: str = DEFAULT_DATASET,
        queue_size: int = 10_000,
        tick_tap: object | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("a Databento API key is required")
        # Called with every live Tick BEFORE bar aggregation, so a feature
        # engine tapping the stream has finished its bookkeeping by the time
        # the closed bar reaches the strategy. None = no tap (default).
        self._tick_tap = tick_tap
        self._api_key = api_key
        self._symbol = symbol
        self._dataset = dataset
        self._queue_size = queue_size
        self._live: object | None = None

    # -- historical ---------------------------------------------------------

    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        """Closed bars in [start, end), built from historical trades.

        The SDK's historical client is synchronous and does real network I/O,
        so it runs on a worker thread rather than blocking the event loop that
        the live stream and every executor share.
        """
        return await asyncio.to_thread(self._get_bars_sync, symbol, timeframe, start, end)

    def _get_bars_sync(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        import databento as db

        client = db.Historical(key=self._api_key)
        store = client.timeseries.get_range(
            dataset=self._dataset,
            start=start,
            end=end,
            symbols=self._symbol,
            schema=db.Schema.TRADES,
            stype_in=db.SType.CONTINUOUS,
        )

        aggregator = BarAggregator(symbol, [timeframe])
        bars: list[Bar] = []
        for record in store:
            if not isinstance(record, db.TradeMsg):
                continue
            bars.extend(aggregator.add_tick(self._to_tick(record, symbol)))
        # Only genuinely closed buckets: flush() marks a partial trailing
        # bucket closed=False, and get_bars promises closed bars.
        bars.extend(bar for bar in aggregator.flush() if bar.closed)
        return [bar for bar in bars if start <= bar.open_time < end]

    def _to_tick(self, record: object, symbol: str) -> Tick:
        side = getattr(record, "side", None)
        return Tick(
            symbol=symbol,
            ts=_timestamp(record.ts_event),  # type: ignore[attr-defined]
            price=_price(record.price),  # type: ignore[attr-defined]
            size=int(record.size),  # type: ignore[attr-defined]
            side=str(getattr(side, "value", side)) if side is not None else None,
        )

    # -- live ---------------------------------------------------------------

    async def stream(
        self, symbol: str, timeframes: list[str], resume_from: datetime | None = None
    ) -> AsyncIterator[Bar]:
        """Closed bars from the live trade stream.

        `resume_from` is honoured by asking the provider to replay intraday
        from that point -- which is what populates warmup window 2 (see
        feed/base.py). A live feed that skipped straight to now would leave
        the gap the process was down for permanently invisible.
        """
        import databento as db

        live = db.Live(key=self._api_key, reconnect_policy=db.ReconnectPolicy.NONE)
        self._live = live
        live.subscribe(
            dataset=self._dataset,
            schema=db.Schema.TRADES,
            symbols=self._symbol,
            stype_in=db.SType.CONTINUOUS,
            start=resume_from,
        )

        aggregator = BarAggregator(symbol, timeframes)
        try:
            async for record in live:
                if isinstance(record, db.ErrorMsg):
                    # Surfaced as an exception so ReconnectingFeed sees a
                    # failure rather than a stream that quietly went silent.
                    raise ConnectionError(f"databento error: {record.err}")
                if not isinstance(record, db.TradeMsg):
                    continue
                tick = self._to_tick(record, symbol)
                if self._tick_tap is not None:
                    self._tick_tap(tick)  # type: ignore[operator]
                for bar in aggregator.add_tick(tick):
                    yield bar
        finally:
            # The trailing partial bucket is deliberately NOT flushed: a
            # stream ending mid-bar has not produced a closed bar, and
            # emitting one would hand the strategy a bar whose period had not
            # finished -- the lookahead bug the whole feed layer exists to
            # prevent.
            await self.close()

    async def close(self) -> None:
        live = self._live
        self._live = None
        if live is None:
            return
        try:
            live.stop()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - teardown never raises
            logger.exception("failed to stop the databento live client")
