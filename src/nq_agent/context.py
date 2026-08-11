from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import date, datetime

from nq_agent.clock import Clock, SessionCalendar
from nq_agent.models import Bar, Position


class Context:
    """Read-only view handed to the strategy on every bar.

    The strategy sees only the properties. The mutators are the engine's, and
    the engine calls record_bar before on_bar, so the bar being processed is
    already the newest entry in its timeframe's history.
    """

    def __init__(self, clock: Clock, calendar: SessionCalendar, history_bars: int) -> None:
        self._clock = clock
        self._calendar = calendar
        self._history_bars = history_bars
        self._bars: dict[str, deque[Bar]] = {}
        self._position: Position | None = None
        self._trades_taken = 0
        self._warmup = False

    def bars(self, timeframe: str, count: int) -> Sequence[Bar]:
        history = self._bars.get(timeframe)
        if history is None:
            return []
        if count >= len(history):
            return list(history)
        return list(history)[-count:]

    @property
    def position(self) -> Position | None:
        return self._position

    @property
    def trades_taken(self) -> int:
        return self._trades_taken

    @property
    def now(self) -> datetime:
        return self._clock.now()

    @property
    def session_date(self) -> date:
        return self._calendar.session_date_for(self._clock.now())

    @property
    def is_warmup(self) -> bool:
        return self._warmup

    def record_bar(self, bar: Bar) -> None:
        history = self._bars.get(bar.timeframe)
        if history is None:
            history = deque(maxlen=self._history_bars)
            self._bars[bar.timeframe] = history
        history.append(bar)

    def set_position(self, position: Position | None) -> None:
        self._position = position

    def set_trades_taken(self, count: int) -> None:
        self._trades_taken = count

    def set_warmup(self, warmup: bool) -> None:
        self._warmup = warmup
