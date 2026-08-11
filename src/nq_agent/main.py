from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime
from pathlib import Path

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.config import Settings, load_settings
from nq_agent.context import Context
from nq_agent.execution.base import Executor
from nq_agent.execution.dryrun import DryRunExecutor, DryRunNotifier
from nq_agent.feed.base import DataFeed
from nq_agent.feed.replay import ReplayFeed
from nq_agent.journal import Journal
from nq_agent.models import Bar, Position, SessionState, Signal, SignalIntent
from nq_agent.position import PositionTracker
from nq_agent.risk.accounts import AccountRegistry
from nq_agent.risk.limits import RiskManager
from nq_agent.router import Router
from nq_agent.session import SessionManager
from nq_agent.state import StateStore
from nq_agent.strategy.always import AlwaysStrategy
from nq_agent.strategy.base import Strategy
from nq_agent.strategy.stub import StubStrategy

logger = logging.getLogger(__name__)

STRATEGIES: dict[str, type[Strategy]] = {"stub": StubStrategy, "always": AlwaysStrategy}


def build_strategy(name: str) -> Strategy:
    try:
        return STRATEGIES[name]()
    except KeyError:
        known = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"unknown strategy '{name}'; known strategies: {known}") from None


def build_executors(settings: Settings) -> list[Executor]:
    """Expand each config entry into one executor instance per account.

    A notify entry has no accounts and becomes a single instance. A dryrun
    entry with no accounts also becomes a single, bare-named instance -- the
    Router's duplicate-name check is what catches that instance colliding
    with a notifier of the same name, which is the intended guard rail, not
    something this function needs to re-implement. Webhook entries are not
    built here -- they arrive with the Databento plan.
    """
    executors: list[Executor] = []
    for entry in settings.executors:
        if not entry.enabled:
            continue
        if entry.type == "notify":
            executors.append(DryRunNotifier(entry.name))
        elif entry.type == "dryrun":
            accounts: list[str | None] = list(entry.accounts) if entry.accounts else [None]
            for account in accounts:
                executors.append(DryRunExecutor(entry.name, account_id=account))
        else:
            raise ValueError(f"executor type '{entry.type}' is not available in this build")
    return executors


class Engine:
    """The bar loop. Everything else is a collaborator."""

    def __init__(
        self,
        settings: Settings,
        feed: DataFeed,
        strategy: Strategy,
        clock: SimClock,
        calendar: SessionCalendar,
        journal: Journal,
        router: Router,
        risk: RiskManager,
        state_store: StateStore,
        resume_from: datetime | None = None,
        resume_position: Position | None = None,
        trades_taken: int = 0,
        resumed_session_date: date | None = None,
        max_bars: int | None = None,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self.trades_taken = trades_taken
        self.is_halted = False
        self._feed = feed
        self._calendar = calendar
        self._journal = journal
        self._router = router
        self._risk = risk
        self._state = state_store
        self._resume_from = resume_from
        self._max_bars = max_bars
        self._tracker = PositionTracker()
        self._tracker.restore(resume_position)
        # resumed_session_date must reach SessionManager, not just this
        # constructor: Strategy.on_session_start unconditionally clears the
        # per-session state that restore_state (called by run_from_config,
        # below) just rebuilt. Without this, the manager starts the resumed
        # session fresh on its first bar instead of adopting it, silently
        # discarding the restore. See SessionManager's docstring.
        self._session = SessionManager(
            strategy, calendar, journal, resumed_session_date=resumed_session_date
        )
        self._context = Context(clock, calendar, settings.context.history_bars)
        self._last_bar_time: datetime | None = None

    async def _persist(self) -> None:
        session_date = self._session.current_session_date
        if session_date is None:
            return
        await self._state.save(
            SessionState(
                session_date=session_date,
                trades_taken=self.trades_taken,
                is_halted=self.is_halted,
                strategy_state=self.strategy.get_state(),
                last_bar_time=self._last_bar_time,
                position=self._tracker.position,
            )
        )

    async def _handle_signal(
        self, signal: Signal, bar: Bar, warmup: bool, *, already_journaled: bool = False
    ) -> None:
        """Route a signal through risk, the journal and the router.

        `already_journaled` is for the cutoff flatten: SessionManager.on_bar
        journals its own "signal_emitted" record before handing the signal back
        (see session.py), because it is the one component besides the strategy
        allowed to generate a signal and it owns that record. A strategy's
        ENTRY signal has no such record -- strategies are pure and have no
        journal access -- so this is the only "signal_emitted" write for those.
        Writing it again here for a flatten would duplicate the same signal_id
        under the same event name.
        """
        session_date = self._session.current_session_date
        assert session_date is not None

        if warmup:
            await self._journal.write(
                "signal_suppressed_backfill",
                session_date,
                signal_id=signal.id,
                intent=signal.intent,
                reason=signal.reason,
            )
            return

        veto = self._risk.check(signal, self.trades_taken, len(self._router.enabled))
        if veto is not None:
            await self._journal.write(
                "risk_veto",
                session_date,
                signal_id=veto.signal_id,
                reason=veto.reason,
                detail=veto.detail,
            )
            return

        if not already_journaled:
            await self._journal.write(
                "signal_emitted",
                session_date,
                signal_id=signal.id,
                source="strategy",
                intent=signal.intent,
                direction=signal.direction,
                quantity=signal.quantity,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                reason=signal.reason,
            )

        await self._router.dispatch(signal, session_date)
        self._risk.record_accepted(signal)

        if signal.intent is SignalIntent.ENTRY:
            self._tracker.on_signal(signal)
            self.trades_taken += 1
            await self._journal.write(
                "position_opened",
                session_date,
                signal_id=signal.id,
                entry_price=signal.entry_price,
            )
        else:
            closed = self._tracker.flatten(bar.close, bar.close_time)
            if closed is not None:
                await self._journal.write(
                    "position_closed",
                    session_date,
                    exit_price=closed.exit_price,
                    exit_reason=closed.exit_reason,
                )

    async def _run_strategy(self, bar: Bar, session_date: date) -> Signal | None:
        """Call the strategy, converting a raise into a halted session.

        A strategy bug must not take the process down mid-position. The session
        stops calling on_bar, but the loop keeps running so the cutoff flatten
        and session_end still happen.
        """
        if self.is_halted:
            return None
        try:
            return self.strategy.on_bar(bar, self._context)
        except Exception as exc:  # noqa: BLE001 - a strategy bug halts, never crashes
            self.is_halted = True
            logger.exception("strategy %s raised", self.strategy.name)
            await self._journal.write(
                "strategy_error",
                session_date,
                strategy=self.strategy.name,
                error=f"{type(exc).__name__}: {exc}",
                bar_close_time=bar.close_time,
            )
            return None

    async def run(self) -> None:
        await self._state.init_schema()
        for executor in self._router.enabled:
            if not await executor.health_check():
                logger.warning("health check failed for %s", executor.name)

        processed = 0
        async for bar in self._feed.stream(
            self.settings.symbol, self.settings.timeframes, self._resume_from
        ):
            warmup = self._resume_from is not None and bar.close_time <= self._resume_from
            self._context.set_warmup(warmup)
            self._last_bar_time = bar.close_time

            closed = self._tracker.on_bar(bar)
            flatten_signal = await self._session.on_bar(bar, self._tracker.position)
            session_date = self._session.current_session_date
            assert session_date is not None

            if closed is not None:
                await self._journal.write(
                    "position_closed",
                    session_date,
                    exit_price=closed.exit_price,
                    exit_reason=closed.exit_reason,
                )

            self._context.record_bar(bar)
            self._context.set_position(self._tracker.position)
            self._context.set_trades_taken(self.trades_taken)

            if flatten_signal is not None:
                await self._handle_signal(flatten_signal, bar, warmup, already_journaled=True)

            strategy_signal = await self._run_strategy(bar, session_date)
            if strategy_signal is not None:
                await self._handle_signal(strategy_signal, bar, warmup)

            if not warmup:
                await self._persist()

            processed += 1
            if self._max_bars is not None and processed >= self._max_bars:
                break

        await self._session.end_session()
        await self._feed.close()


async def run_from_config(
    config_path: Path,
    replay_path: Path,
    strategy_name: str,
    max_bars: int | None,
    strategy_override: Strategy | None = None,
) -> Engine:
    settings = load_settings(config_path)
    strategy = strategy_override or build_strategy(strategy_name)

    calendar = SessionCalendar(
        settings.session.timezone, settings.session.open, settings.session.cutoff
    )

    state_store = StateStore(settings.state_db_path)
    await state_store.init_schema()

    first_tick = ReplayFeed(replay_path, settings.symbol).first_tick_time()
    session_date = calendar.session_date_for(first_tick)
    prior = await state_store.load(session_date)

    resume_from: datetime | None = None
    resume_position: Position | None = None
    trades_taken = 0
    resumed_session_date: date | None = None
    if prior is not None:
        strategy.restore_state(prior.strategy_state)
        resume_from = prior.last_bar_time
        resume_position = prior.position
        trades_taken = prior.trades_taken
        resumed_session_date = prior.session_date

    clock = SimClock(first_tick)
    journal = Journal(settings.journal_dir, clock)
    feed = ReplayFeed(replay_path, settings.symbol, clock=clock)

    assert settings.risk.kill_switch_path is not None
    risk = RiskManager(
        calendar=calendar,
        max_trades_per_day=settings.risk.max_trades_per_day,
        duplicate_window_seconds=settings.risk.duplicate_window_seconds,
        kill_switch_path=settings.risk.kill_switch_path,
    )
    registry = AccountRegistry(settings.data_dir / "accounts.yaml")
    router = Router(
        build_executors(settings),
        journal,
        settings.router.executor_timeout_seconds,
        settings.router.notify_timeout_seconds,
        settings.router.partial_fan,
        enabled_accounts=registry.enabled_accounts,
    )

    engine = Engine(
        settings=settings,
        feed=feed,
        strategy=strategy,
        clock=clock,
        calendar=calendar,
        journal=journal,
        router=router,
        risk=risk,
        state_store=state_store,
        resume_from=resume_from,
        resume_position=resume_position,
        trades_taken=trades_taken,
        resumed_session_date=resumed_session_date,
        max_bars=max_bars,
    )
    await engine.run()
    return engine


def main() -> None:
    parser = argparse.ArgumentParser(prog="nq_agent")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--strategy", default="always", choices=sorted(STRATEGIES))
    parser.add_argument(
        "--max-bars",
        type=int,
        default=None,
        help="stop after N bars; used to simulate a mid-session kill",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_from_config(args.config, args.replay, args.strategy, args.max_bars))


if __name__ == "__main__":
    main()
