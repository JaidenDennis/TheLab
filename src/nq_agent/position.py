from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from nq_agent.models import Bar, Direction, Position, PositionClose, Signal, SignalIntent


class PositionTracker:
    """Simulated position state, driven by signals and bars.

    When a single bar's range touches both the stop and the target, the stop
    wins. Bar data cannot tell us which came first, so the tracker takes the
    pessimistic reading rather than guessing.

    Every modelling choice here breaks the same way on purpose -- against the
    position:

    - both levels touched in one bar: stop wins
    - a bar opening through the stop: fills at the open, not the stop
    - a bar opening through the target: still fills at the target

    Read P&L off this and it is a floor, not a forecast. What it still does
    not model is commission, exchange fees, and the spread crossed on entry;
    a backtest built on it is gross of all three.
    """

    def __init__(self) -> None:
        self._position: Position | None = None

    @property
    def position(self) -> Position | None:
        return self._position

    def restore(self, position: Position | None) -> None:
        self._position = position

    def on_signal(self, signal: Signal) -> Position | None:
        """Open a position from an ENTRY signal, or refuse.

        Returns the opened Position, or None if the signal was refused --
        wrong intent, or a position is already open. None is a real outcome
        the caller must observe, not swallow: a second ENTRY while one is
        already open is not a no-op anywhere else in the system (risk still
        clears it, the router still dispatches it to the broker), so the
        caller needs to know its own local bookkeeping did not follow suit.
        """
        if signal.intent is not SignalIntent.ENTRY or self._position is not None:
            return None
        assert signal.entry_price is not None
        assert signal.stop_price is not None
        assert signal.target_price is not None
        self._position = Position(
            symbol=signal.symbol,
            direction=signal.direction,
            quantity=signal.quantity,
            entry_price=signal.entry_price,
            entry_time=signal.timestamp,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
        )
        return self._position

    def on_bar(self, bar: Bar) -> PositionClose | None:
        position = self._position
        if position is None or bar.symbol != position.symbol:
            return None

        if not position.is_managed:
            # Adopted from the broker during reconciliation: real, but with
            # no stop or target, because the broker reports what is held and
            # never what the exit was meant to be. There is nothing to test a
            # bar against. It stays open until the cutoff flatten closes it,
            # which is the whole reason adoption exists -- an unmanaged
            # position that nothing tracks runs overnight.
            return None

        assert position.stop_price is not None
        assert position.target_price is not None
        if position.direction is Direction.LONG:
            stop_hit = bar.low <= position.stop_price
            target_hit = bar.high >= position.target_price
            # A bar that OPENS through the stop never traded at the stop
            # price: the market gapped past it while we were not looking, and
            # the first price actually available is the open. Filling at the
            # stop books a loss that was not on offer -- the one optimistic
            # assumption the "stop wins" pessimism did not cover, and the one
            # that flatters a backtest exactly where real slippage is worst.
            gapped = bar.open < position.stop_price
        else:
            stop_hit = bar.high >= position.stop_price
            target_hit = bar.low <= position.target_price
            gapped = bar.open > position.stop_price

        if stop_hit:
            # Deliberately not symmetric with the target below: a gapped-through
            # target still fills at the target, because understating a win is
            # the safe direction to be wrong in. Overstating one is not.
            fill = bar.open if gapped else position.stop_price
            return self._close(position, fill, bar.close_time, "STOP")
        if target_hit:
            return self._close(position, position.target_price, bar.close_time, "TARGET")
        return None

    def flatten(self, price: Decimal, at: datetime) -> PositionClose | None:
        position = self._position
        if position is None:
            return None
        return self._close(position, price, at, "FLATTEN")

    def _close(
        self, position: Position, price: Decimal, at: datetime, reason: str
    ) -> PositionClose:
        self._position = None
        return PositionClose(
            position=position, exit_price=price, exit_time=at, exit_reason=reason
        )
