from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from nq_agent.clock import SessionCalendar
from nq_agent.models import Direction, Position, RiskVeto, Signal, SignalIntent, VetoReason


@dataclass(frozen=True)
class Breach:
    """A money limit currently breached, with the figure that tripped it.

    The detail is computed alongside the decision, not reconstructed by the
    caller, so the number journaled is always the number that was measured.
    """

    reason: VetoReason
    detail: str


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
        max_daily_loss: Decimal | None = None,
        max_trailing_drawdown: Decimal | None = None,
        trailing_drawdown_basis: Literal["equity", "closed"] = "equity",
    ) -> None:
        self._calendar = calendar
        self._max_trades = max_trades_per_day
        self._window = timedelta(seconds=duplicate_window_seconds)
        self._kill_switch_path = kill_switch_path
        self._recent: list[tuple[datetime, str, Direction]] = []
        # Both limits are magnitudes, not signed floors: "500" means "stop
        # after losing 500". Reading them as signed would invert the control,
        # which is the failure direction that ends an account.
        self._max_daily_loss = abs(max_daily_loss) if max_daily_loss is not None else None
        self._max_drawdown = (
            abs(max_trailing_drawdown) if max_trailing_drawdown is not None else None
        )
        # The Literal type is the validation: config.py declares the same
        # Literal, so an unknown basis is rejected at config load, and a
        # direct constructor call with a bad string is a type error.
        self._basis = trailing_drawdown_basis
        self._daily_pnl = Decimal("0")
        self._equity = Decimal("0")
        self._high_water = Decimal("0")
        # Mark-to-market on the open position. Kept apart from _equity, which
        # is realised only, because the two are reset on different events: a
        # close realises the open figure and must not leave it double-counted.
        self._unrealised = Decimal("0")
        # What the open position was already marked at when today's session
        # started. The daily limit measures today's damage, so a position
        # carried across the boundary must not charge yesterday's open move
        # against today's fresh limit -- only the move since this mark counts.
        self._session_open_mark = Decimal("0")
        self._reconciliation_detail: str | None = None

    @property
    def unrealised(self) -> Decimal:
        return self._unrealised

    def mark_to_market(
        self, position: Position | None, price: Decimal, point_value: Decimal
    ) -> None:
        """Value the open position at `price`, in account currency.

        Called on every bar. A prop firm's trailing limit watches equity
        including the open position, so a limit measured on closed trades
        alone can sit comfortably inside itself while the firm is closing the
        account -- the position is down 800, the agent has realised nothing,
        and both of its numbers look fine.

        `position is None` clears the figure: once a position closes its
        result is realised through record_realised_pnl, and leaving the mark
        behind would count the same money twice.
        """
        if position is None:
            self._unrealised = Decimal("0")
            # The mark belonged to the position that just closed. A new
            # position opened later today starts measuring from zero, not
            # from an offset left behind by a different trade.
            self._session_open_mark = Decimal("0")
        else:
            self._unrealised = position.pnl(price, point_value)

        if self._basis == "equity":
            # An unrealised peak moves the mark. This is the rule that
            # surprises people: being up 1000 on an open trade and giving it
            # back is a 1000 drawdown, even though nothing was ever realised.
            self._high_water = max(self._high_water, self._equity + self._unrealised)

    def breach(self) -> Breach | None:
        """Which money limit is currently breached, if any.

        Shared by check() -- which turns it into a veto on new entries -- and
        the engine, which uses it to decide whether to flatten. Vetoing is not
        a sufficient response when a position is already open.

        The daily figure counts the open position's move since the session
        started, not since entry: a position carried across the boundary has
        yesterday's damage in yesterday's figure already, and the firm's
        per-day limit is measuring today. The drawdown figure uses the full
        since-entry mark, because equity is measured over the life of the
        account, not the day.
        """
        if self._max_daily_loss is not None:
            open_today = self._unrealised - self._session_open_mark
            daily = self._daily_pnl + open_today
            if -daily >= self._max_daily_loss:
                return Breach(
                    VetoReason.DAILY_LOSS_LIMIT,
                    f"down {-daily} on the session "
                    f"(realised {self._daily_pnl}, open move today {open_today}), "
                    f"limit is {self._max_daily_loss}",
                )
        if self._max_drawdown is not None:
            equity = self._equity + self._unrealised
            if self._high_water - equity >= self._max_drawdown:
                return Breach(
                    VetoReason.TRAILING_DRAWDOWN,
                    f"{self._high_water - equity} below the {self._high_water} "
                    f"high-water mark (open {self._unrealised}), "
                    f"limit is {self._max_drawdown}",
                )
        return None

    def require_reconciliation(self, detail: str) -> None:
        """Block new entries until the agent and the broker agree again.

        Set when a dispatch comes back UNKNOWN, or when a reconciliation pass
        finds a structural divergence. Trading on top of a position you are
        not sure you hold turns one unknown into two.
        """
        self._reconciliation_detail = detail

    def clear_reconciliation(self) -> None:
        """Called only when a reconciliation pass actually agrees. Never on a
        timer, and never because the operator waited a while."""
        self._reconciliation_detail = None

    @property
    def reconciliation_required(self) -> bool:
        return self._reconciliation_detail is not None

    def record_realised_pnl(self, amount: Decimal) -> None:
        """Book a closed trade's result, in account currency.

        Realised only. Open-position P&L deliberately does not move these
        numbers: a limit that trips on an unrealised swing would flatten a
        position that had not actually lost anything yet.
        """
        self._daily_pnl += amount
        self._equity += amount
        self._high_water = max(self._high_water, self._equity)

    def start_session(self, session_date: date) -> None:
        """Roll the day. Resets the daily figure and nothing else.

        The trailing drawdown deliberately survives: it is measured over the
        life of the account, not the day, and the prop firm is not resetting
        it overnight either.
        """
        self._daily_pnl = Decimal("0")
        # A position still open at the boundary re-bases here: its move so
        # far belongs to the session that just ended, and only what it does
        # from this mark on counts against the new day's limit.
        self._session_open_mark = self._unrealised

    def snapshot(self) -> dict[str, str]:
        """Serialisable P&L state, for the same reason SessionState exists.

        A restart that forgets the day's losses is a restart that resumes
        trading straight through the limit that had just stopped it.
        """
        return {
            "daily_pnl": str(self._daily_pnl),
            "equity": str(self._equity),
            "high_water": str(self._high_water),
            # Without this, a restart mid-session with a carried position
            # re-bases its mark to zero and charges the whole since-entry
            # move against today's limit again.
            "session_open_mark": str(self._session_open_mark),
            # The duplicate-signal window too: it is in-memory only otherwise,
            # so a process that restarts inside the window comes back with an
            # empty one and waves through the exact repeat it exists to stop.
            # A crash-loop is precisely when a strategy re-fires the same
            # signal, so the guard is blind at the moment it matters most.
            "recent": json.dumps(
                [
                    [ts.isoformat(), symbol, direction.value]
                    for ts, symbol, direction in self._recent
                ]
            ),
        }

    def restore(self, state: dict[str, str] | None) -> None:
        if not state:
            return
        # str() before Decimal: this arrives back through JSON.
        self._daily_pnl = Decimal(str(state.get("daily_pnl", "0")))
        self._equity = Decimal(str(state.get("equity", "0")))
        self._high_water = Decimal(str(state.get("high_water", "0")))
        self._session_open_mark = Decimal(str(state.get("session_open_mark", "0")))
        self._recent = [
            (datetime.fromisoformat(ts), symbol, Direction(direction))
            for ts, symbol, direction in json.loads(str(state.get("recent", "[]")))
        ]

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._window
        self._recent = [entry for entry in self._recent if entry[0] > cutoff]

    def _is_duplicate(self, signal: Signal) -> bool:
        self._prune(signal.timestamp)
        return any(
            symbol == signal.symbol and direction == signal.direction
            for _, symbol, direction in self._recent
        )

    def record_accepted(self, signal: Signal) -> None:
        if signal.intent is SignalIntent.ENTRY:
            self._recent.append((signal.timestamp, signal.symbol, signal.direction))
            # Prune here too: pruning only inside check() would let _recent grow
            # without bound if a caller ever records without checking first.
            self._prune(signal.timestamp)

    def check(
        self, signal: Signal, trades_taken: int, enabled_executor_count: int
    ) -> RiskVeto | None:
        if signal.intent is SignalIntent.FLATTEN:
            return None

        def veto(reason: VetoReason, detail: str) -> RiskVeto:
            return RiskVeto(signal_id=signal.id, reason=reason, detail=detail)

        if self._kill_switch_path.exists():
            return veto(VetoReason.KILL_SWITCH, f"halt file present at {self._kill_switch_path}")

        # Immediately after the kill switch and ahead of everything else: if
        # the agent does not know what it holds, no other check is meaningful.
        # Position size, trade count and P&L are all computed from a belief
        # that is currently in doubt.
        if self._reconciliation_detail is not None:
            return veto(VetoReason.RECONCILIATION_REQUIRED, self._reconciliation_detail)

        if not self._calendar.is_session_open(signal.timestamp):
            if self._calendar.is_before_cutoff(signal.timestamp):
                return veto(VetoReason.SESSION_CLOSED, "signal arrived before session open")
            return veto(VetoReason.PAST_CUTOFF, "signal arrived at or after session cutoff")

        # Money checks before the trade-count check: hitting a loss limit is
        # the more serious condition and should be the reason journaled when
        # both apply.
        breached = self.breach()
        if breached is not None:
            return veto(breached.reason, breached.detail)

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
