from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import Clock, RealClock, SessionCalendar, SimClock
from nq_agent.config import Settings, load_settings
from nq_agent.context import Context
from nq_agent.execution.base import Executor, NotifyExecutor
from nq_agent.execution.dryrun import DryRunExecutor, DryRunNotifier
from nq_agent.feed.base import DataFeed
from nq_agent.feed.replay import ReplayFeed
from nq_agent.journal import Journal
from nq_agent.models import (
    Bar,
    DivergenceKind,
    Position,
    PositionClose,
    SessionState,
    Signal,
    SignalIntent,
)
from nq_agent.position import PositionTracker
from nq_agent.reconcile import PositionSource, Reconciler
from nq_agent.risk.accounts import AccountRegistry
from nq_agent.risk.limits import RiskManager
from nq_agent.router import Router
from nq_agent.session import SessionManager, build_protective_flatten, write_signal_emitted
from nq_agent.state import StateStore
from nq_agent.strategy.always import AlwaysStrategy
from nq_agent.strategy.base import Strategy
from nq_agent.strategy.naim import NoiseAreaIntradayMomentum
from nq_agent.strategy.orb import OpeningRangeBreakout
from nq_agent.strategy.sme import SessionMomentumExpansion
from nq_agent.strategy.stub import StubStrategy

logger = logging.getLogger(__name__)

STRATEGIES: dict[str, type[Strategy]] = {
    "stub": StubStrategy,
    "always": AlwaysStrategy,
    # A reference implementation, not a validated edge -- see orb.py's module
    # docstring before pointing this at an account.
    "orb": OpeningRangeBreakout,
    # Session-Momentum Expansion v1. Implemented to spec with documented
    # deviations (see sme.py's module docstring); NOT yet validated -- the
    # walk-forward protocol in the spec gates any live use.
    "sme": SessionMomentumExpansion,
    # Noise-area intraday momentum (spec NAIM v1.0). Needs precomputed noise
    # curves to trade anything -- without them every day is a no-trade day --
    # so production use goes through the runner that loads them.
    "naim": NoiseAreaIntradayMomentum,
}


def build_strategy(name: str) -> Strategy:
    try:
        return STRATEGIES[name]()
    except KeyError:
        known = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"unknown strategy '{name}'; known strategies: {known}") from None


@dataclass(frozen=True)
class FeedBinding:
    """A feed, the clock that agrees with it, and the two instants that define
    the resume windows.

    This is the seam a live feed arrives through. Before it existed,
    run_from_config named ReplayFeed and SimClock directly and derived the
    session to resume from the replay fixture's first tick -- so wiring a real
    feed meant editing that function rather than implementing DataFeed, and a
    live process (whose "first tick" is whenever it happened to start) had no
    way to say which session it was resuming.

    `anchor` is the instant that identifies which session's persisted state to
    load: the fixture's first tick for a replay, `now` for a live process.

    `resume_latest` says the anchor does NOT name the session to resume, and
    the latest persisted session should be loaded instead. A replay sets it:
    its anchor is the fixture's first tick, so a multi-day fixture killed in
    its third session would otherwise restore the first session's completed
    state and re-run days already traded. A live process leaves it False --
    `now` is exactly the session it is resuming.

    `backfill_until` is the wall-clock moment the live stream opened, i.e. the
    far edge of the gap the process was down for. Everything between the last
    persisted bar and that moment is warmup window 2 -- replayed for real
    through strategy and position tracker, but never dispatched, because its
    moment has passed. It is None for a replay, whose restart is a
    deterministic continuation rather than a catch-up. See feed/base.py's
    stream() docstring for all three windows.
    """

    feed: DataFeed
    clock: Clock
    anchor: datetime
    backfill_until: datetime | None = None
    resume_latest: bool = False


def replay_binding(replay_path: Path, symbol: str) -> FeedBinding:
    """Deterministic playback of a tick fixture, anchored on its first tick."""
    anchor = ReplayFeed(replay_path, symbol).first_tick_time()
    clock = SimClock(anchor)
    return FeedBinding(
        feed=ReplayFeed(replay_path, symbol, clock=clock),
        clock=clock,
        anchor=anchor,
        resume_latest=True,
    )


def live_binding(settings: Settings) -> FeedBinding:
    """Databento, wrapped in the reconnect policy, on a real clock.

    `anchor` and `backfill_until` are both `now`: the session to resume is
    whichever one is running as this process starts, and everything between
    the last persisted bar and this moment is the gap the process was down
    for -- warmup window 2, replayed through the strategy but never
    dispatched. See feed/base.py.
    """
    from nq_agent.feed.databento import DatabentoFeed
    from nq_agent.feed.reconnecting import ReconnectingFeed

    if not settings.databento_api_key:
        raise ValueError(
            "NQ_DATABENTO_API_KEY is not set. The live feed cannot start without it; "
            "pass --replay to run against a fixture instead."
        )

    clock = RealClock()
    now = clock.now()
    feed = ReconnectingFeed(
        DatabentoFeed(
            api_key=settings.databento_api_key,
            symbol=settings.databento_symbol,
            dataset=settings.databento_dataset,
        ),
        max_attempts=settings.feed.max_reconnect_attempts,
        initial_backoff=settings.feed.initial_backoff_seconds,
        max_backoff=settings.feed.max_backoff_seconds,
    )
    return FeedBinding(feed=feed, clock=clock, anchor=now, backfill_until=now)


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
        accounts: list[str | None] = list(entry.accounts) if entry.accounts else [None]
        if entry.type == "notify":
            executors.append(DryRunNotifier(entry.name))
        elif entry.type == "dryrun":
            for account in accounts:
                executors.append(DryRunExecutor(entry.name, account_id=account))
        elif entry.type == "webhook":
            from nq_agent.execution.webhook import WebhookExecutor

            if not settings.webhook_url:
                raise ValueError(
                    f"executor {entry.name!r} is type webhook but NQ_WEBHOOK_URL is not set"
                )
            for account in accounts:
                executors.append(
                    WebhookExecutor(
                        entry.name,
                        url=settings.webhook_url,
                        token=settings.webhook_token,
                        account_id=account,
                        timeout=settings.router.executor_timeout_seconds,
                    )
                )
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
        # Clock, not SimClock: a live wiring supplies a RealClock, and nothing
        # in the engine needs simulation -- it only ever calls now().
        clock: Clock,
        calendar: SessionCalendar,
        journal: Journal,
        router: Router,
        risk: RiskManager,
        state_store: StateStore,
        resume_from: datetime | None = None,
        backfill_until: datetime | None = None,
        resume_position: Position | None = None,
        trades_taken: int = 0,
        is_halted: bool = False,
        resumed_session_date: date | None = None,
        max_bars: int | None = None,
        position_source: PositionSource | None = None,
        reconciler: Reconciler | None = None,
        reconcile_interval_bars: int = 0,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self.trades_taken = trades_taken
        # Restored from prior.is_halted on resume by run_from_config, not
        # hardcoded -- a strategy that halted before a crash must not come
        # back up silently un-halted. Reset to False on a genuine session
        # rollover, alongside trades_taken -- see run()'s rollover check.
        self.is_halted = is_halted
        # feed and router are public: run_from_config hands the Engine back to
        # its caller, and the collaborators it was wired with are part of what
        # that caller legitimately inspects (the existing callers already read
        # strategy, trades_taken and is_halted the same way).
        self.feed = feed
        self.router = router
        self._calendar = calendar
        self._journal = journal
        self._risk = risk
        self._state = state_store
        self._resume_from = resume_from
        # backfill_until is the wall-clock moment a live feed's own catch-up
        # (resume_from -> now) ends. None means there is no such window --
        # the replay case, where a restart is a deterministic continuation
        # of the same fixture rather than a gap that needs to be caught up.
        # See feed/base.py's stream() docstring for the three windows this
        # and resume_from together define.
        self._backfill_until = backfill_until
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
        self._clock = clock
        # Reconciliation is opt-in: without a position source there is
        # nothing to ask, and every existing replay and backtest runs exactly
        # as before. When one is wired, the broker becomes the source of
        # truth for what is held.
        self._position_source = position_source
        self._reconciler = reconciler
        self._reconcile_interval = reconcile_interval_bars
        self._resumed_session_date = resumed_session_date

    async def _persist(self, session_date: date | None = None) -> None:
        if session_date is None:
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
                risk_state=self._risk.snapshot(),
            )
        )

    async def _end_session_and_persist(self) -> None:
        """End the session, then persist what ending it changed.

        Strategy.on_session_end is where cross-session statistics get banked
        (a rolling daily range, an OFI distribution -- whatever the strategy
        accumulates), and it runs AFTER the last bar's persist. Without this
        follow-up save, everything a strategy learns at session end evaporates
        on process exit, and a multi-fixture backtest resumes every morning
        with the long memory it was supposed to have built overnight --
        which surfaced as a strategy whose 20-session warmup never completed
        across 947 sessions.
        """
        session_date = self._session.current_session_date
        await self._session.end_session()
        if session_date is not None:
            await self._persist(session_date)

    async def reconcile(self, session_date: date, trigger: str) -> bool:
        """Ask the broker what it holds and act on the answer.

        Returns True when the agent and the broker agree. The three triggers
        that matter are startup (the agent has just restored a belief it has
        not checked), an UNKNOWN dispatch (the belief may have just diverged),
        and an interval (a broker-side stop fill the agent cannot see).

        A fetch that fails is itself a reason to stop trading: not knowing
        the answer is the same risk as knowing it is wrong.
        """
        if self._position_source is None or self._reconciler is None:
            return True

        try:
            broker_positions = await self._position_source.fetch_positions()
        except Exception as exc:  # noqa: BLE001 - a failed query blocks, never crashes
            logger.exception("could not fetch broker positions")
            detail = f"broker position query failed: {type(exc).__name__}: {exc}"
            self._risk.require_reconciliation(detail)
            await self._journal.write(
                "reconciliation_failed", session_date, trigger=trigger, error=detail
            )
            return False

        report = self._reconciler.reconcile(
            self._tracker.position, broker_positions, at=self._clock.now()
        )

        if not report.is_blocking:
            # Slippage only, or full agreement. Adopt the broker's fill price
            # so P&L -- and therefore every money limit -- is measured against
            # what was actually paid rather than what the strategy asked for.
            price = report.consensus_price
            current = self._tracker.position
            if price is not None and current is not None and current.entry_price != price:
                self._tracker.restore(current.model_copy(update={"entry_price": price}))
                await self._journal.write(
                    "reconciliation_adjusted",
                    session_date,
                    trigger=trigger,
                    agent_price=current.entry_price,
                    broker_price=price,
                )
            self._risk.clear_reconciliation()
            await self._journal.write("reconciliation_ok", session_date, trigger=trigger)
            return True

        summary = report.summary()
        self._risk.require_reconciliation(summary)
        await self._journal.write(
            "reconciliation_divergence",
            session_date,
            trigger=trigger,
            detail=summary,
            accounts=report.blocking_accounts,
        )

        adopted = report.adoptable_position
        if adopted is not None:
            # The broker holds something the agent did not know about. Take
            # ownership so the cutoff flatten can close it -- otherwise
            # nothing believes it exists and it runs overnight. Adoption is
            # not approval: entries stay blocked, and the adopted position
            # has no stop or target because none is known.
            self._tracker.restore(adopted)
            await self._journal.write(
                "reconciliation_adopted",
                session_date,
                direction=adopted.direction,
                quantity=adopted.quantity,
                entry_price=adopted.entry_price,
            )
        elif self._tracker.position is not None and all(
            d.kind is DivergenceKind.AGENT_ONLY for d in report.divergences
        ):
            # Every account says flat and the agent thinks otherwise. Drop the
            # phantom rather than keep flattening something that is not there.
            await self._journal.write(
                "reconciliation_dropped_phantom",
                session_date,
                direction=self._tracker.position.direction,
                quantity=self._tracker.position.quantity,
            )
            self._tracker.restore(None)

        await self._alert(f"reconciliation divergence: {summary}")
        return False

    async def _risk_flatten(self, bar: Bar, session_date: date) -> Signal | None:
        """Emit a protective FLATTEN when a money limit is breached.

        Vetoing entries is the right response to a limit when you are flat and
        no response at all when you are not: the position is already on, and
        the only thing that helps is getting out. A limit that watches equity
        including the open trade -- which is what a prop firm's trailing
        drawdown does -- is meaningless without this.

        Deliberately does not touch the cutoff flatten. SessionManager owns
        that one and has already returned it by the time this is called, so
        this only ever fires when the session is otherwise still running.
        """
        position = self._tracker.position
        if position is None:
            return None
        breached = self._risk.breach()
        if breached is None:
            return None

        if self._risk.reconciliation_required:
            # The position this flatten would close is exactly the belief
            # reconciliation has flagged as doubtful. If the broker is in
            # fact flat, the "flatten" is a real order that opens a fresh,
            # unmanaged position nobody is watching. Withheld, not skipped:
            # the breach re-fires on every bar, so the flatten goes out on
            # the first bar after a reconciliation pass agrees -- and the
            # session cutoff flatten, which deliberately does go out on a
            # doubtful belief rather than hold overnight, is the backstop.
            await self._journal.write(
                "risk_flatten_withheld",
                session_date,
                reason=breached.reason,
                detail=breached.detail,
            )
            return None

        signal = build_protective_flatten(
            position, bar.close_time, f"risk limit breached: {breached.reason.value}"
        )
        await self._journal.write(
            "risk_flatten",
            session_date,
            signal_id=signal.id,
            reason=breached.reason,
            detail=breached.detail,
            unrealised=self._risk.unrealised,
        )
        await write_signal_emitted(self._journal, session_date, signal, source="risk")
        # Blocks new entries as well: the limit that just forced an exit is
        # still breached on the next bar, and FLATTEN bypasses the veto so
        # this does not prevent the exit itself.
        self.is_halted = True
        return signal

    async def _alert(self, message: str) -> None:
        for executor in self.router.enabled:
            if isinstance(executor, NotifyExecutor):
                try:
                    await executor.alert(message)
                except Exception:  # noqa: BLE001 - an alert never breaks the run
                    logger.exception("alert failed on %s", executor.name)

    def _realised_pnl(self, closed: PositionClose) -> Decimal:
        """What that trade actually did to the account, in currency.

        The risk layer counts trades; this is what turns a close into the
        money figure the loss and drawdown limits are measured in. Commission
        is charged here rather than left to the report, because a limit that
        ignores costs stops trading later than the broker's own numbers do.
        """
        contract = self.settings.contract
        gross = closed.position.pnl(closed.exit_price, contract.point_value)
        return gross - contract.commission_per_round_turn * closed.position.quantity

    async def _journal_close(self, closed: PositionClose, session_date: date) -> None:
        """Write a closed position as a whole trade, entry side included.

        An exit price and a reason describe half a trade. Anything reading the
        journal back -- the backtest report, a live P&L check, the LLM
        filter's shadow-mode evaluation later -- needs the entry it closed
        against, and reconstructing that by pairing each close with an earlier
        position_opened record is exactly the kind of stateful re-derivation
        that goes wrong on the days it matters.
        """
        realised = self._realised_pnl(closed)
        self._risk.record_realised_pnl(realised)
        await self._journal.write(
            "position_closed",
            session_date,
            direction=closed.position.direction,
            quantity=closed.position.quantity,
            entry_price=closed.position.entry_price,
            entry_time=closed.position.entry_time,
            exit_price=closed.exit_price,
            exit_time=closed.exit_time,
            exit_reason=closed.exit_reason,
            realised_pnl=realised,
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

        veto = self._risk.check(signal, self.trades_taken, len(self.router.enabled))
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

        results = await self.router.dispatch(signal, session_date)
        self._risk.record_accepted(signal)

        # An UNKNOWN leg means the agent's belief about this order may already
        # be wrong. Ask the broker before anything else is sent.
        if any(result.needs_reconciliation for result in results):
            unknown = [r.executor_name for r in results if r.needs_reconciliation]
            self._risk.require_reconciliation(
                f"order {signal.id} returned UNKNOWN on {', '.join(unknown)}"
            )
            await self.reconcile(session_date, trigger="unknown_order")

        if signal.intent is SignalIntent.ENTRY:
            opened = self._tracker.on_signal(signal)
            if opened is None:
                # The tracker refused -- a position is already open. Risk
                # already cleared this signal and the router already
                # dispatched it to the broker above: the broker received a
                # second entry (a reversal, from its point of view) that our
                # own local bookkeeping did not follow. That disagreement
                # must be observable, not silently absorbed by skipping the
                # trades_taken increment with no record of why.
                await self._journal.write(
                    "position_open_rejected",
                    session_date,
                    signal_id=signal.id,
                    reason="position already open",
                )
            else:
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
                await self._journal_close(closed, session_date)

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

    def _reset_after_rollover(self, prior_session_date: date | None) -> None:
        """Zero the day-scoped counters when SessionManager's on_bar call just
        rolled the session date over.

        trades_taken and is_halted live on Engine; SessionManager owns the day
        boundary and exposes it only through current_session_date, so this is
        a before/after comparison around each call to session.on_bar rather
        than a signal SessionManager pushes.

        `prior_session_date is None` is the engine's first-ever bar, which is
        a rollover in disguise when the state that was restored belongs to an
        EARLIER session than the bar that just arrived -- a run resuming
        yesterday's state on today's data, which is exactly what each fixture
        after the first is in a multi-fixture backtest. Yesterday's
        trades_taken, is_halted and daily risk figure must not gate today's
        trading: one halted day would silently poison every later day of a
        backtest. Only a first bar that lands IN the resumed session (the
        adoption case) keeps the restored counters, which is the reason they
        were persisted at all.
        """
        current = self._session.current_session_date
        if current is None:
            return
        rolled = (
            current != prior_session_date
            if prior_session_date is not None
            else self._resumed_session_date is not None
            and current != self._resumed_session_date
        )
        if rolled:
            self.trades_taken = 0
            self.is_halted = False
            # Rolls the daily loss figure only. The trailing drawdown is
            # measured over the life of the account and deliberately
            # survives the boundary -- see RiskManager.start_session.
            self._risk.start_session(current)

    async def _teardown(self) -> None:
        """Close the session and release every collaborator's resources.

        Each step is independent: one failing must not skip the rest, and none
        of them may mask an exception already on its way out of run(). A feed
        that died is exactly when teardown matters most, so this is the one
        place in the engine that swallows what it catches -- loudly.
        """
        steps: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
            ("end_session", self._end_session_and_persist),
            ("feed.close", self.feed.close),
            ("router.close", self.router.close),
        )
        for name, step in steps:
            try:
                await step()
            except Exception:  # noqa: BLE001 - teardown never raises
                logger.exception("teardown step %s failed", name)

    async def run(self) -> None:
        await self._state.init_schema()
        for executor in self.router.enabled:
            if not await executor.health_check():
                logger.warning("health check failed for %s", executor.name)

        # Before the first bar: the agent has just restored a belief about
        # what it holds from its own database and has not checked it against
        # anything. If the crash happened mid-dispatch, that belief is
        # exactly what is most likely to be wrong.
        if self._position_source is not None and self._resumed_session_date is not None:
            await self.reconcile(self._resumed_session_date, trigger="startup")

        try:
            await self._bar_loop()
        except Exception as exc:
            # A feed that drops mid-stream is the most likely live failure
            # there is, and it used to leave no trace: the teardown below sat
            # after the loop rather than in a finally, so the exception skipped
            # session_end and feed.close() on its way out, and nothing ever
            # wrote the feed_error event the design names. Journalled here,
            # then re-raised -- a run that died has not succeeded, and the
            # supervisor that restarts the process needs to see it fail.
            session_date = self._session.current_session_date
            if session_date is not None:
                await self._journal.write(
                    "feed_error",
                    session_date,
                    error=f"{type(exc).__name__}: {exc}",
                    last_bar_time=self._last_bar_time,
                )
            raise
        finally:
            await self._teardown()

    async def _bar_loop(self) -> None:
        processed = 0
        skipped = 0
        since_reconcile = 0
        async for bar in self.feed.stream(
            self.settings.symbol, self.settings.timeframes, self._resume_from
        ):
            # Three windows, not two -- see feed/base.py's stream() docstring.
            # 1. skip: close_time <= resume_from. Already reflected in the
            #    state restore_state/PositionTracker.restore just rebuilt.
            #    Processing it again would double-count it.
            # 2. warmup: past resume_from, at or before backfill_until (a
            #    live feed's real downtime gap). Strategy and tracker run for
            #    real so in-memory state catches up, but any signal's moment
            #    has already passed, so it is suppressed rather than routed.
            # 3. live: everything else. ReplayFeed never sets backfill_until,
            #    so window 2 is empty for it and every post-resume_from bar
            #    lands directly in this one, by design.
            skip = self._resume_from is not None and bar.close_time <= self._resume_from
            warmup = (
                not skip
                and self._backfill_until is not None
                and bar.close_time <= self._backfill_until
            )
            self._context.set_warmup(warmup)
            self._last_bar_time = bar.close_time

            if skip:
                skipped += 1
                # Still let SessionManager see the bar: session adoption (and
                # a same-run rollover the skip window happens to cross) reads
                # current_session_date off it, and the call has no strategy
                # or position side effect of its own. Any flatten signal it
                # returns here is for a bar already reflected in the state
                # already restored, so it is discarded rather than handled.
                #
                # -- except bars from sessions BEFORE the one being resumed.
                # A multi-day replay's skip window starts at the top of the
                # fixture, and those sessions are wholly in the past: handing
                # their bars to SessionManager would burn its one-shot
                # adoption on the first old session, and each rollover on the
                # way forward would clobber the restored trades_taken,
                # is_halted and risk figures. The resumed session must be the
                # first one the manager ever sees.
                if (
                    self._resumed_session_date is None
                    or self._calendar.session_date_for(bar.close_time)
                    >= self._resumed_session_date
                ):
                    prior_session_date = self._session.current_session_date
                    await self._session.on_bar(bar, self._tracker.position)
                    self._reset_after_rollover(prior_session_date)
                processed += 1
                if self._max_bars is not None and processed >= self._max_bars:
                    break
                continue

            closed = self._tracker.on_bar(bar)
            prior_session_date = self._session.current_session_date
            flatten_signal = await self._session.on_bar(bar, self._tracker.position)
            self._reset_after_rollover(prior_session_date)
            session_date = self._session.current_session_date
            assert session_date is not None

            if closed is not None:
                await self._journal_close(closed, session_date)

            # Mark the open position before anything decides anything. Every
            # money limit is measured on equity including it, so a stale mark
            # means the checks below run on last bar's account.
            self._risk.mark_to_market(
                self._tracker.position, bar.close, self.settings.contract.point_value
            )

            self._context.record_bar(bar)
            self._context.set_position(self._tracker.position)
            self._context.set_trades_taken(self.trades_taken)

            if flatten_signal is None and not warmup:
                flatten_signal = await self._risk_flatten(bar, session_date)

            if flatten_signal is not None:
                await self._handle_signal(flatten_signal, bar, warmup, already_journaled=True)

            strategy_signal = await self._run_strategy(bar, session_date)
            if strategy_signal is not None:
                await self._handle_signal(strategy_signal, bar, warmup)

            if not warmup:
                await self._persist()
                # Interval reconciliation catches what nothing else can: a
                # broker-side stop or margin close the agent never saw. Off
                # by default (0) because it costs an API call per interval
                # and means nothing without a position source.
                since_reconcile += 1
                if self._reconcile_interval and since_reconcile >= self._reconcile_interval:
                    since_reconcile = 0
                    await self.reconcile(session_date, trigger="interval")

            processed += 1
            if self._max_bars is not None and processed >= self._max_bars:
                break

        if skipped:
            session_date = self._session.current_session_date
            if session_date is not None:
                await self._journal.write("backfill_skipped", session_date, count=skipped)


def order_accounts(settings: Settings) -> list[str]:
    """Accounts that actually receive orders, in config order.

    Derived from the executors rather than from AccountRegistry: the registry
    is an enable/disable overlay that answers "may this account trade right
    now", and an account switched off mid-session still holds whatever it
    already had. Reconciliation has to check those too -- an account disabled
    while holding a position is precisely the one nobody is watching.

    Notify entries contribute nothing: a human being told about a signal is
    not an account with a position in it.
    """
    accounts: list[str] = []
    for entry in settings.executors:
        if entry.type == "notify":
            continue
        for account in entry.accounts:
            if account not in accounts:
                accounts.append(account)
    return accounts


def check_timeframes(strategy: Strategy, settings: Settings) -> None:
    """Reject a strategy that asks for data this config does not produce.

    required_timeframes was declarative only: nothing compared it against the
    feed's timeframes, so a strategy declaring 15m against a [1m, 5m] config
    simply never fired. That failure is invisible -- no error, no warning, a
    clean run and an empty journal, indistinguishable from a strategy that
    correctly found no setups all day.
    """
    missing = [tf for tf in strategy.required_timeframes if tf not in settings.timeframes]
    if missing:
        raise ValueError(
            f"strategy '{strategy.name}' requires timeframe(s) {missing} that the "
            f"config does not produce (timeframes: {settings.timeframes}); it would "
            "never fire"
        )


def check_live_safety(settings: Settings) -> None:
    """Refuse to start a real-money run with no money limits configured.

    The P&L limits are opt-in, which is correct for a replay and a backtest --
    and means a live config that simply omits them starts happily with no
    daily loss limit and no drawdown limit at all. On a prop account those
    two are the difference between a bad day and a closed account, and their
    absence is invisible: nothing looks wrong until the firm's own limit is
    breached, and by then the account is gone.

    Scoped to webhook executors specifically, because that is what makes a run
    real. A dry run with no limits is fine.
    """
    live_executors = [e for e in settings.executors if e.enabled and e.type == "webhook"]
    if not live_executors:
        return

    missing = [
        name
        for name, value in (
            ("risk.max_daily_loss", settings.risk.max_daily_loss),
            ("risk.max_trailing_drawdown", settings.risk.max_trailing_drawdown),
        )
        if value is None
    ]
    if missing:
        names = ", ".join(e.name for e in live_executors)
        raise ValueError(
            f"executor(s) {names} send real orders, but {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not set. Set them below your "
            "prop firm's own limits, or disable the executor. Refusing to trade "
            "an account with no loss limit."
        )


async def run_from_config(
    config_path: Path,
    replay_path: Path | None,
    strategy_name: str,
    max_bars: int | None,
    strategy_override: Strategy | None = None,
    backfill_until: datetime | None = None,
    binding: FeedBinding | None = None,
    position_source: PositionSource | None = None,
) -> Engine:
    """Load config, restore prior state if any, and run one session.

    The feed and clock arrive as a `binding` (see FeedBinding), not as
    hardcoded types: pass one explicitly, or pass `replay_path` to get the
    replay binding, or pass neither to get the live binding once one exists.

    `strategy_override` exists so tests can inject a strategy the CLI has no
    name for. `backfill_until` overrides the binding's own value so tests can
    exercise warmup window 2 without a live feed to produce it -- production
    callers leave it None and let the binding decide. Production paths always
    go through build_strategy.
    """
    settings = load_settings(config_path)
    strategy = strategy_override or build_strategy(strategy_name)
    check_timeframes(strategy, settings)
    check_live_safety(settings)

    if binding is None:
        binding = (
            replay_binding(replay_path, settings.symbol)
            if replay_path is not None
            else live_binding(settings)
        )

    calendar = SessionCalendar(
        settings.session.timezone, settings.session.open, settings.session.cutoff
    )

    state_store = StateStore(settings.state_db_path)
    await state_store.init_schema()

    # Which persisted session is being resumed. A live process resumes the
    # session running now -- its anchor. A replay resumes wherever the killed
    # run actually got to: its anchor is the fixture's first tick, which
    # names the file's first session regardless of how far that run was.
    if binding.resume_latest:
        prior = await state_store.load_latest()
    else:
        prior = await state_store.load(calendar.session_date_for(binding.anchor))

    resume_from: datetime | None = None
    resume_position: Position | None = None
    trades_taken = 0
    is_halted = False
    resumed_session_date: date | None = None
    if prior is not None:
        strategy.restore_state(prior.strategy_state)
        resume_from = prior.last_bar_time
        resume_position = prior.position
        trades_taken = prior.trades_taken
        is_halted = prior.is_halted
        resumed_session_date = prior.session_date

    clock = binding.clock
    journal = Journal(settings.journal_dir, clock)
    feed = binding.feed
    if backfill_until is None:
        backfill_until = binding.backfill_until

    assert settings.risk.kill_switch_path is not None
    risk = RiskManager(
        calendar=calendar,
        max_trades_per_day=settings.risk.max_trades_per_day,
        duplicate_window_seconds=settings.risk.duplicate_window_seconds,
        kill_switch_path=settings.risk.kill_switch_path,
        max_daily_loss=settings.risk.max_daily_loss,
        max_trailing_drawdown=settings.risk.max_trailing_drawdown,
        trailing_drawdown_basis=settings.risk.trailing_drawdown_basis,
    )
    if prior is not None:
        risk.restore(prior.risk_state)
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
        backfill_until=backfill_until,
        resume_position=resume_position,
        trades_taken=trades_taken,
        is_halted=is_halted,
        resumed_session_date=resumed_session_date,
        max_bars=max_bars,
        position_source=position_source,
        reconciler=(
            Reconciler(settings.symbol, order_accounts(settings))
            if position_source is not None
            else None
        ),
        reconcile_interval_bars=settings.risk.reconcile_interval_bars,
    )
    await engine.run()
    return engine


def main() -> None:
    parser = argparse.ArgumentParser(prog="nq_agent")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="tick fixture to replay; omit to use the live feed (see live_binding)",
    )
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
