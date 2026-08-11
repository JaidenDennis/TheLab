from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from nq_agent.clock import SessionCalendar
from nq_agent.models import Direction, RiskVeto, Signal, SignalIntent, VetoReason


class RiskManager:
    """Sits between strategy and router. Vetoes only, never modifies a signal.

    FLATTEN bypasses every check. A kill switch that traps you in an open
    position is worse than no kill switch.
    """

    def __init__(
        self,
        calendar: SessionCalendar,
        max_trades_per_day: int,
        duplicate_window_seconds: int,
        kill_switch_path: Path,
    ) -> None:
        self._calendar = calendar
        self._max_trades = max_trades_per_day
        self._window = timedelta(seconds=duplicate_window_seconds)
        self._kill_switch_path = kill_switch_path
        self._recent: list[tuple[datetime, str, Direction]] = []

    def _is_duplicate(self, signal: Signal) -> bool:
        cutoff = signal.timestamp - self._window
        self._recent = [entry for entry in self._recent if entry[0] > cutoff]
        return any(
            symbol == signal.symbol and direction == signal.direction
            for _, symbol, direction in self._recent
        )

    def record_accepted(self, signal: Signal) -> None:
        if signal.intent is SignalIntent.ENTRY:
            self._recent.append((signal.timestamp, signal.symbol, signal.direction))

    def check(
        self, signal: Signal, trades_taken: int, enabled_executor_count: int
    ) -> RiskVeto | None:
        if signal.intent is SignalIntent.FLATTEN:
            return None

        def veto(reason: VetoReason, detail: str) -> RiskVeto:
            return RiskVeto(signal_id=signal.id, reason=reason, detail=detail)

        if self._kill_switch_path.exists():
            return veto(VetoReason.KILL_SWITCH, f"halt file present at {self._kill_switch_path}")

        if not self._calendar.is_session_open(signal.timestamp):
            if self._calendar.is_before_cutoff(signal.timestamp):
                return veto(VetoReason.SESSION_CLOSED, "signal arrived before session open")
            return veto(VetoReason.PAST_CUTOFF, "signal arrived at or after session cutoff")

        if trades_taken >= self._max_trades:
            return veto(
                VetoReason.MAX_TRADES,
                f"{trades_taken} trades already taken, limit is {self._max_trades}",
            )

        if enabled_executor_count == 0:
            return veto(VetoReason.ACCOUNT_DISABLED, "no enabled executor for any account")

        if self._is_duplicate(signal):
            return veto(
                VetoReason.DUPLICATE_SIGNAL,
                f"same symbol and direction within {self._window.total_seconds():.0f}s",
            )

        return None
