from __future__ import annotations

from datetime import date

from nq_agent.clock import SessionCalendar
from nq_agent.journal import Journal
from nq_agent.models import Bar, Position, Signal, SignalIntent
from nq_agent.strategy.base import Strategy


class SessionManager:
    """Owns session lifecycle and the cutoff flatten.

    This is the only component besides the strategy allowed to generate a
    signal. Flatten is generation, not veto, so it cannot live in the risk
    layer without breaking that layer's one invariant.

    `resumed_session_date` names the session a crashed run was mid-way
    through. A caller resuming that run restores strategy state before the
    engine loop starts; Strategy.on_session_start unconditionally clears
    exactly that state (see its docstring), so calling it for the resumed
    session would silently discard the restore on the very first bar. When
    the first bar this instance ever sees falls on that date, the session is
    adopted instead of started — see `_adopt`. Every session after the first
    behaves exactly as if `resumed_session_date` had never been passed.
    """

    def __init__(
        self,
        strategy: Strategy,
        calendar: SessionCalendar,
        journal: Journal,
        resumed_session_date: date | None = None,
    ) -> None:
        self._strategy = strategy
        self._calendar = calendar
        self._journal = journal
        self._session_date: date | None = None
        self._flattened_for: date | None = None
        self._resumed_session_date = resumed_session_date
        # One-shot: only the manager's first-ever session transition can be a
        # resume. Cleared on that transition regardless of outcome, so a later
        # rollover that happens to land on this same date is never mistaken
        # for one.
        self._resume_pending = resumed_session_date is not None

    @property
    def current_session_date(self) -> date | None:
        return self._session_date

    async def _start(self, session_date: date) -> None:
        self._session_date = session_date
        self._strategy.on_session_start(session_date)
        await self._journal.write(
            "session_start", session_date, strategy=self._strategy.name
        )

    async def _adopt(self, session_date: date) -> None:
        """Resume an in-progress session without resetting strategy state.

        Skips on_session_start (see the class docstring for why) and journals
        the distinction so the record shows this session was resumed rather
        than started fresh.
        """
        self._session_date = session_date
        await self._journal.write(
            "session_resumed", session_date, strategy=self._strategy.name
        )

    async def end_session(self) -> None:
        if self._session_date is None:
            return
        finished = self._session_date
        self._strategy.on_session_end(finished)
        await self._journal.write("session_end", finished, strategy=self._strategy.name)
        self._session_date = None

    async def on_bar(self, bar: Bar, position: Position | None) -> Signal | None:
        session_date = self._calendar.session_date_for(bar.close_time)

        if self._session_date != session_date:
            resuming = self._resume_pending and session_date == self._resumed_session_date
            self._resume_pending = False
            if resuming:
                await self._adopt(session_date)
            else:
                await self.end_session()
                await self._start(session_date)

        if position is None:
            return None
        if self._flattened_for == session_date:
            return None
        if bar.close_time < self._calendar.cutoff_utc(session_date):
            return None

        self._flattened_for = session_date
        signal = Signal(
            timestamp=bar.close_time,
            symbol=position.symbol,
            intent=SignalIntent.FLATTEN,
            direction=position.direction,
            quantity=position.quantity,
            reason="session cutoff reached with an open position",
        )
        await self._journal.write(
            "signal_emitted",
            session_date,
            signal_id=signal.id,
            source="session_manager",
            intent=signal.intent,
            direction=signal.direction,
            quantity=signal.quantity,
            reason=signal.reason,
        )
        return signal
