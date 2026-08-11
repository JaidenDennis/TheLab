from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from nq_agent.models import Bar, Direction, Position, PositionClose, Signal, SignalIntent


class PositionTracker:
    """Simulated position state, driven by signals and bars.

    When a single bar's range touches both the stop and the target, the stop
    wins. Bar data cannot tell us which came first, so the tracker takes the
    pessimistic reading rather than guessing.
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

        if position.direction is Direction.LONG:
            stop_hit = bar.low <= position.stop_price
            target_hit = bar.high >= position.target_price
        else:
            stop_hit = bar.high >= position.stop_price
            target_hit = bar.low <= position.target_price

        if stop_hit:
            return self._close(position, position.stop_price, bar.close_time, "STOP")
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
