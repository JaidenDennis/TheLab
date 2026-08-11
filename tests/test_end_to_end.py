import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nq_agent.config import load_settings
from nq_agent.context import Context
from nq_agent.main import run_from_config
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.state import StateStore
from nq_agent.strategy.always import AlwaysStrategy
from nq_agent.strategy.base import Strategy

FIXTURE = Path("tests/fixtures/2026-07-15.jsonl")
SESSION = date(2026, 7, 15)


class EveryBarStrategy(Strategy):
    """Fires on every 1m bar. Exists to make warmup suppression observable."""

    name = "every_bar"
    required_timeframes = ["1m"]

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe != "1m":
            return None
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            direction=Direction.LONG,
            entry_price=bar.close,
            stop_price=bar.close - Decimal("10"),
            target_price=bar.close + Decimal("20"),
            quantity=1,
            reason="every bar",
        )

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {}

    def restore_state(self, state: dict[str, Any]) -> None:
        return None


class ExplodingStrategy(Strategy):
    """Raises on the third bar. Exists to prove the engine halts rather than dies."""

    name = "exploding"
    required_timeframes = ["1m"]

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        self.calls += 1
        if self.calls == 3:
            raise RuntimeError("strategy exploded")
        return None

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {"calls": self.calls}

    def restore_state(self, state: dict[str, Any]) -> None:
        self.calls = int(state.get("calls", 0))


class AccumulatingStrategy(Strategy):
    """Counts 1m bars and sums their closes. Never trades.

    Exists to make double-counting observable. AlwaysStrategy's `{"fired":
    bool}` state is idempotent -- replaying a bar it has already seen leaves
    it looking identical to a correct resume, which is exactly why it cannot
    pin C1. An accumulator cannot hide that: if a bar already reflected in
    restored state is replayed through on_bar, bars_seen and sum_close both
    grow past what an uninterrupted run over the same bars produces.
    """

    name = "accumulating"
    required_timeframes = ["1m"]

    def __init__(self) -> None:
        self.bars_seen = 0
        self.sum_close = Decimal("0")

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe != "1m":
            return None
        self.bars_seen += 1
        self.sum_close += bar.close
        return None

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {"bars_seen": self.bars_seen, "sum_close": str(self.sum_close)}

    def restore_state(self, state: dict[str, Any]) -> None:
        self.bars_seen = int(state.get("bars_seen", 0))
        self.sum_close = Decimal(str(state.get("sum_close", "0")))


class CallCountingStrategy(Strategy):
    """Records the close_time of every on_bar call it receives.

    Exists to assert directly on which bars the engine let through to the
    strategy, independent of any accumulation semantics.
    """

    name = "call_counting"
    required_timeframes = ["1m"]

    def __init__(self) -> None:
        self.calls: list[datetime] = []

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe == "1m":
            self.calls.append(bar.close_time)
        return None

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {"call_count": len(self.calls)}

    def restore_state(self, state: dict[str, Any]) -> None:
        return None


class DelayedEntryStrategy(Strategy):
    """Enters LONG on the Nth 1m bar it sees, then never again.

    AlwaysStrategy always enters on the session's very first bar, so there is
    never a bar before its entry -- it cannot pin C3, which is specifically
    about bars that predate the position's own entry being wrongly evaluated
    against it. This strategy delays its entry so a test can construct bars
    before it with a low that would trip the eventual stop if (incorrectly)
    evaluated against a position that does not exist yet.
    """

    name = "delayed_entry"
    required_timeframes = ["1m"]

    def __init__(
        self, entry_after: int, stop_offset: Decimal, target_offset: Decimal
    ) -> None:
        self._entry_after = entry_after
        self._stop_offset = stop_offset
        self._target_offset = target_offset
        self._seen = 0
        self._fired = False

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe != "1m" or self._fired:
            return None
        self._seen += 1
        if self._seen != self._entry_after:
            return None
        self._fired = True
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            intent=SignalIntent.ENTRY,
            direction=Direction.LONG,
            entry_price=bar.close,
            stop_price=bar.close - self._stop_offset,
            target_price=bar.close + self._target_offset,
            quantity=1,
            reason="delayed entry",
        )

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {"seen": self._seen, "fired": self._fired}

    def restore_state(self, state: dict[str, Any]) -> None:
        self._seen = int(state.get("seen", 0))
        self._fired = bool(state.get("fired", False))


class LongThenShortStrategy(Strategy):
    """Enters LONG on the first 1m bar, SHORT on the second, then never again.

    Exists for I2: PositionTracker.on_signal silently refuses the SHORT
    (a position is already open), but nothing about risk or the router knows
    that -- both signals clear risk and both dispatch. Measured behaviour:
    "two position_opened records, two broker dispatches, trades_taken=2, and
    one actual tracked position." Wide, mismatched offsets on both sides so
    neither position's stop or target is at risk of being touched by the
    fixture's own price action before the test's assertions run.
    """

    name = "long_then_short"
    required_timeframes = ["1m"]

    def __init__(self) -> None:
        self._calls = 0

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe != "1m":
            return None
        self._calls += 1
        if self._calls == 1:
            return Signal(
                timestamp=bar.close_time,
                symbol=bar.symbol,
                intent=SignalIntent.ENTRY,
                direction=Direction.LONG,
                entry_price=bar.close,
                stop_price=bar.close - Decimal("5000"),
                target_price=bar.close + Decimal("5000"),
                quantity=1,
                reason="long then short: first bar",
            )
        if self._calls == 2:
            return Signal(
                timestamp=bar.close_time,
                symbol=bar.symbol,
                intent=SignalIntent.ENTRY,
                direction=Direction.SHORT,
                entry_price=bar.close,
                stop_price=bar.close + Decimal("5000"),
                target_price=bar.close - Decimal("5000"),
                quantity=1,
                reason="long then short: second bar",
            )
        return None

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {"calls": self._calls}

    def restore_state(self, state: dict[str, Any]) -> None:
        self._calls = int(state.get("calls", 0))


def settings_for(tmp_path: Path) -> Path:
    """Write a paper config whose data_dir points at tmp_path."""
    config = tmp_path / "test.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify, mff, fundednext]\n"
        "  - name: dryrun_notify\n"
        "    type: notify\n"
        "    enabled: true\n"
        "    accounts: []\n"
    )
    return config


def journal_events(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "journal" / "2026-07-15.jsonl"
    return [json.loads(line) for line in path.read_text().strip().splitlines()]


def journal_events_for(tmp_path: Path, session_date: date) -> list[dict[str, object]]:
    """Like journal_events, but for an arbitrary session date -- journal_events
    is hardcoded to SESSION (2026-07-15) and a multi-day fixture needs more
    than one day's file.
    """
    path = tmp_path / "journal" / f"{session_date.isoformat()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines()]


async def test_stub_strategy_runs_a_full_session_without_trading(tmp_path: Path) -> None:
    engine = await run_from_config(settings_for(tmp_path), FIXTURE, "stub", None)

    events = {event["event"] for event in journal_events(tmp_path)}
    assert "session_start" in events
    assert "session_end" in events
    assert "signal_emitted" not in events
    assert engine.trades_taken == 0


async def test_always_strategy_reaches_every_executor(tmp_path: Path) -> None:
    engine = await run_from_config(settings_for(tmp_path), FIXTURE, "always", None)

    results = [e for e in journal_events(tmp_path) if e["event"] == "order_result"]
    names = {str(e["executor_name"]) for e in results}
    assert names == {
        "dryrun_notify",
        "dryrun_broker:tradeify",
        "dryrun_broker:mff",
        "dryrun_broker:fundednext",
    }
    assert all(e["success"] is True for e in results)
    assert engine.trades_taken == 1


async def test_notify_result_is_journaled_before_the_broker_results(tmp_path: Path) -> None:
    await run_from_config(settings_for(tmp_path), FIXTURE, "always", None)

    results = [e for e in journal_events(tmp_path) if e["event"] == "order_result"]
    assert str(results[0]["executor_name"]) == "dryrun_notify"


async def test_signal_carries_absolute_prices(tmp_path: Path) -> None:
    await run_from_config(settings_for(tmp_path), FIXTURE, "always", None)

    emitted = next(e for e in journal_events(tmp_path) if e["event"] == "signal_emitted")
    assert emitted["intent"] == SignalIntent.ENTRY.value
    assert "entry_price" in emitted
    assert "stop_price" in emitted
    assert "target_price" in emitted


async def test_kill_switch_vetoes_the_entry(tmp_path: Path) -> None:
    config = settings_for(tmp_path)
    (tmp_path / "nq-agent.halt").write_text("halt")

    engine = await run_from_config(config, FIXTURE, "always", None)

    vetoes = [e for e in journal_events(tmp_path) if e["event"] == "risk_veto"]
    assert vetoes
    assert vetoes[0]["reason"] == "KILL_SWITCH"
    assert engine.trades_taken == 0


async def test_state_is_persisted_at_the_end_of_the_run(tmp_path: Path) -> None:
    config = settings_for(tmp_path)
    await run_from_config(config, FIXTURE, "always", None)

    store = StateStore(load_settings(config).state_db_path)
    await store.init_schema()
    state = await store.load(SESSION)

    assert state is not None
    assert state.trades_taken == 1
    assert state.strategy_state == {"fired": True}
    assert state.last_bar_time is not None


async def test_restart_resumes_without_refiring_the_entry(tmp_path: Path) -> None:
    config = settings_for(tmp_path)

    first = await run_from_config(config, FIXTURE, "always", max_bars=60)
    assert first.trades_taken == 1

    second = await run_from_config(config, FIXTURE, "always", max_bars=None)

    entries = [
        e
        for e in journal_events(tmp_path)
        if e["event"] == "signal_emitted" and e["intent"] == SignalIntent.ENTRY.value
    ]
    assert len(entries) == 1, "the morning's entry must not fire twice"
    assert second.trades_taken == 1


async def test_restart_rebuilds_strategy_state(tmp_path: Path) -> None:
    config = settings_for(tmp_path)
    await run_from_config(config, FIXTURE, "always", max_bars=60)
    engine = await run_from_config(config, FIXTURE, "always", max_bars=None)
    assert engine.strategy.get_state() == {"fired": True}


# `test_backfill_signals_are_suppressed_and_journaled` originally asserted the
# opposite of what is correct and has been replaced by the two tests below.
# Its premise was that replaying a fixture from the top after a resume *is*
# the backfill window, so bars 1..resume_from should be re-run through the
# strategy with signals suppressed. That is C1/C2/C3: those bars are already
# reflected in the state that was just restored, so the engine must skip
# them outright, not re-run and suppress them -- and with ReplayFeed (which
# always replays from the top and never sets backfill_until) there is no
# real catch-up window at all, so no signal is ever suppressed on a plain
# resume. `test_a_resumed_run_produces_no_suppressed_signals` below pins
# that directly; `test_bars_within_the_backfill_window_are_suppressed_not_dispatched`
# proves suppression still works on the window it actually applies to: a
# live feed's real resume_from..backfill_until gap, represented here by
# passing backfill_until explicitly since no live feed exists yet to supply
# one.


async def test_a_resumed_run_produces_no_suppressed_signals(tmp_path: Path) -> None:
    config = settings_for(tmp_path)

    await run_from_config(
        config, FIXTURE, "always", max_bars=10, strategy_override=EveryBarStrategy()
    )
    await run_from_config(
        config, FIXTURE, "always", max_bars=20, strategy_override=EveryBarStrategy()
    )

    suppressed = [
        e for e in journal_events(tmp_path) if e["event"] == "signal_suppressed_backfill"
    ]
    assert not suppressed, (
        "ReplayFeed never sets backfill_until, so a plain resume has no catch-up "
        "window to suppress -- bars up to resume_from are skipped outright instead"
    )


async def test_bars_within_the_backfill_window_are_suppressed_not_dispatched(
    tmp_path: Path,
) -> None:
    """Pins C2: a bar after resume_from is not automatically live.

    Before this fix, everything past resume_from was dispatched immediately,
    because there was no window 2 to catch it -- against a real feed that
    backfills its own downtime gap before going live, that doubles every
    order the feed's own backfill had already placed once. This constructs
    that gap explicitly via backfill_until, since no live feed exists yet to
    produce one.
    """
    config = settings_for(tmp_path)

    # AlwaysStrategy, not EveryBarStrategy, for this first leg: it fires
    # exactly once, so trades_taken is unambiguous here regardless of I2 (a
    # separate finding, fixed after this one) -- this test only needs a
    # cold-start dispatch to compare against, not repeated-entry semantics.
    first = await run_from_config(
        config, FIXTURE, "always", max_bars=5, strategy_override=AlwaysStrategy()
    )
    assert first.trades_taken == 1
    before = len([e for e in journal_events(tmp_path) if e["event"] == "order_result"])
    assert before > 0, "precondition: the cold-start bar must have actually dispatched"

    store = StateStore(load_settings(config).state_db_path)
    await store.init_schema()
    state = await store.load(SESSION)
    assert state is not None and state.last_bar_time is not None
    backfill_until = state.last_bar_time + timedelta(minutes=5)

    second = await run_from_config(
        config,
        FIXTURE,
        "always",
        max_bars=10,
        strategy_override=EveryBarStrategy(),
        backfill_until=backfill_until,
    )

    after = len([e for e in journal_events(tmp_path) if e["event"] == "order_result"])
    assert after == before, "a bar inside the backfill window must not reach an executor"
    assert second.trades_taken == first.trades_taken, (
        "a suppressed backfill signal must not be counted as a trade"
    )

    suppressed = [
        e for e in journal_events(tmp_path) if e["event"] == "signal_suppressed_backfill"
    ]
    assert suppressed, "a bar inside the backfill window must journal its suppressed signal"


async def test_backfill_never_reaches_an_executor(tmp_path: Path) -> None:
    config = settings_for(tmp_path)

    await run_from_config(
        config, FIXTURE, "always", max_bars=10, strategy_override=EveryBarStrategy()
    )
    before = len([e for e in journal_events(tmp_path) if e["event"] == "order_result"])

    await run_from_config(
        config, FIXTURE, "always", max_bars=10, strategy_override=EveryBarStrategy()
    )
    after = len([e for e in journal_events(tmp_path) if e["event"] == "order_result"])

    assert after == before, "a replayed bar must not produce a new order"


async def test_a_raising_strategy_halts_the_session_instead_of_crashing(
    tmp_path: Path,
) -> None:
    engine = await run_from_config(
        settings_for(tmp_path), FIXTURE, "stub", None, strategy_override=ExplodingStrategy()
    )

    events = journal_events(tmp_path)
    assert any(e["event"] == "strategy_error" for e in events)
    assert any(e["event"] == "session_end" for e in events)
    assert engine.is_halted is True


async def test_unknown_strategy_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        await run_from_config(settings_for(tmp_path), FIXTURE, "nonexistent", None)


# --- Self-review additions ---
#
# The brief's own restart tests (above) pass for the wrong reason if
# `resumed_session_date` is never threaded into SessionManager: on a resumed
# run, SessionManager falls back to a normal `_start`, which calls
# `Strategy.on_session_start` and wipes the `restore_state` that
# `run_from_config` just did. AlwaysStrategy then looks freshly armed and
# fires again on the very first (warmup) bar of the replay -- but that bar is
# within the warmup window, so `_handle_signal` suppresses it as
# "signal_suppressed_backfill" before it ever reaches "signal_emitted" or the
# router. The re-arm-then-immediately-resuppress sequence happens to leave
# `_fired` and `trades_taken` exactly where a correct resume would too, so
# `test_restart_resumes_without_refiring_the_entry` and
# `test_restart_rebuilds_strategy_state` both pass either way. This test
# targets the one place the two paths are actually observable: whether the
# second run adopts the session (`session_resumed`, no re-arming attempt at
# all) or silently starts it over (`session_start` again, plus a suppressed
# backfill signal as evidence the strategy was wrongly re-armed).


async def test_resume_adopts_the_session_without_rearming_the_strategy(tmp_path: Path) -> None:
    config = settings_for(tmp_path)
    await run_from_config(config, FIXTURE, "always", max_bars=60)
    await run_from_config(config, FIXTURE, "always", max_bars=None)

    events = journal_events(tmp_path)
    assert sum(1 for e in events if e["event"] == "session_start") == 1, (
        "the resumed run must not start the session over"
    )
    assert sum(1 for e in events if e["event"] == "session_resumed") == 1, (
        "the resumed run must adopt the in-progress session"
    )
    assert not any(e["event"] == "signal_suppressed_backfill" for e in events), (
        "a correctly restored strategy is already fired and must not attempt to re-enter "
        "during the backfill replay, not even a suppressed attempt"
    )


# A position that is still open when the session cutoff arrives must be
# flattened, and that flatten is a real signal -- it must clear risk (FLATTEN
# bypasses every check, but must still reach the router) exactly like an
# entry does. AlwaysStrategy's default $10/$20 offsets are tight enough that
# the fixture's random walk resolves the trade via stop or target well before
# 16:30 in the unmodified run, so this test widens the offsets far past the
# fixture's realistic range to force the position to survive to the close.


async def test_cutoff_flatten_is_routed_when_a_position_survives_to_the_close(
    tmp_path: Path,
) -> None:
    wide_strategy = AlwaysStrategy(stop_offset=Decimal("10000"), target_offset=Decimal("10000"))
    engine = await run_from_config(
        settings_for(tmp_path), FIXTURE, "always", None, strategy_override=wide_strategy
    )

    events = journal_events(tmp_path)
    flattens = [
        e
        for e in events
        if e["event"] == "signal_emitted" and e["intent"] == SignalIntent.FLATTEN.value
    ]
    # Exactly one, not two: SessionManager.on_bar journals its own
    # signal_emitted record for the flatten it generates before ever handing
    # the signal back to the engine (see session.py). The engine must not
    # write a second signal_emitted record under the same signal_id when it
    # goes on to route that same signal -- an ENTRY signal (which comes from
    # the strategy, with no journal access of its own) is the only case that
    # needs the engine's own write.
    assert len(flattens) == 1, "the flatten's signal_emitted must not be journaled twice"

    closes = [e for e in events if e["event"] == "position_closed"]
    assert closes, "the flatten must actually close the tracked position"
    assert closes[-1]["exit_reason"] == "FLATTEN"

    results = [e for e in events if e["event"] == "order_result"]
    # Two real signals reach the router over the session: the entry and the
    # cutoff flatten. Four executors each, so eight results total -- not four.
    assert len(results) == 8, "the flatten signal must be dispatched, not just journaled"
    assert all(e["success"] is True for e in results)
    assert engine.trades_taken == 1, "a FLATTEN must not be counted as a new trade"


def settings_with_high_trade_limit(tmp_path: Path) -> Path:
    """Like settings_for, but raises max_trades_per_day so it cannot veto anything.

    Exists for test_warmup_suppression_blocks_dispatch_even_when_risk_would_allow_it,
    which needs a replayed signal that risk would accept, so that only the warmup
    check itself stands between it and the router.
    """
    config = tmp_path / "test.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "risk:\n"
        "  max_trades_per_day: 100\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify, mff, fundednext]\n"
        "  - name: dryrun_notify\n"
        "    type: notify\n"
        "    enabled: true\n"
        "    accounts: []\n"
    )
    return config


async def test_warmup_suppression_blocks_dispatch_even_when_risk_would_allow_it(
    tmp_path: Path,
) -> None:
    """Prove the `skip` branch in `Engine.run` blocks dispatch entirely, not
    just the strategy call.

    The bar in question here is a `skip` bar (its close_time equals
    resume_from exactly), so it is never handed to the strategy at all --
    there is no signal for `_handle_signal`'s warmup branch to suppress in
    this scenario. (For that, see
    test_bars_within_the_backfill_window_are_suppressed_not_dispatched,
    which forces a real warmup window via backfill_until.) What this test
    isolates is that `test_backfill_never_reaches_an_executor` alone cannot
    tell "skipped" apart from "risk vetoed it anyway": with the default
    max_trades_per_day=2 and EveryBarStrategy firing on every 1m bar, the
    first call already pins trades_taken at the daily cap, so even a bug that
    deleted the whole `skip` branch would still show zero new dispatches on
    the second call, MAX_TRADES vetoing every bar that reached the strategy.
    This test raises the trade limit so risk would accept a re-dispatched
    signal, so only the skip actually stands between it and the router.
    Mutation-verified: reintroducing the old warmup predicate (bar.close_time
    <= resume_from, with no separate skip window) makes this scenario leak 4
    new order_result records; with the fix in place, it leaks none.
    """
    config = settings_with_high_trade_limit(tmp_path)

    first = await run_from_config(
        config, FIXTURE, "always", max_bars=1, strategy_override=EveryBarStrategy()
    )
    assert first.trades_taken == 1
    before = len([e for e in journal_events(tmp_path) if e["event"] == "order_result"])
    assert before > 0, "precondition: the cold-start bar must have actually dispatched"

    second = await run_from_config(
        config, FIXTURE, "always", max_bars=1, strategy_override=EveryBarStrategy()
    )
    after = len([e for e in journal_events(tmp_path) if e["event"] == "order_result"])

    assert after == before, "a replayed bar must not produce a new order, even if risk allows it"
    assert second.trades_taken == 1, "a suppressed backfill signal must not be counted as a trade"


# The engine calls `self._tracker.on_bar(bar)` (evaluate stop/target against
# this bar) before `self._session.on_bar(bar, self._tracker.position)`
# (evaluate the cutoff flatten), passing the tracker's post-exit position.
# That order matters exactly once: when a position's stop or target and the
# session cutoff land on the very same bar. Every other test in this file
# either never reaches the cutoff with a position open (AlwaysStrategy's
# default $10/$20 offsets resolve well before 16:30) or reaches it with wide
# offsets that guarantee survival to the close (the wide_strategy test
# above), so neither exercises this coincidence. Mutation-verified: swapping
# the two calls (so the session sees the stale, still-open position) leaves
# every other test in this entire suite green, this one is the only one that
# catches it.
#
# A minute-long custom fixture is built here, rather than reusing the
# session-long FIXTURE, because forcing a stop hit to land on a specific
# minute against FIXTURE's random walk is not practical to engineer
# reliably; a session shrunk to two minutes makes bar 2 both the stop hit
# and the cutoff by construction.


def two_minute_session_settings(tmp_path: Path) -> tuple[Path, Path]:
    ticks = tmp_path / "ticks.jsonl"
    ticks.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-07-15T13:30:00+00:00", "price": "100.00", "size": 1}),
                json.dumps({"ts": "2026-07-15T13:30:30+00:00", "price": "100.00", "size": 1}),
                # bar 1 (13:30-13:31) closes flat at 100.00: AlwaysStrategy(stop_offset=1)
                # enters LONG at entry=100.00, stop=99.00, target=1100.00 (unreachable).
                json.dumps({"ts": "2026-07-15T13:31:00+00:00", "price": "100.00", "size": 1}),
                # bar 2 (13:31-13:32) dips to 98.00: low <= stop(99.00), a genuine stop
                # hit. Its close_time, 13:32:00 UTC, is *also* this session's cutoff
                # (09:32 America/New_York, configured below) -- the coincidence this
                # test exists to force.
                json.dumps({"ts": "2026-07-15T13:31:30+00:00", "price": "98.00", "size": 1}),
            ]
        )
        + "\n"
    )

    config = tmp_path / "test.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m]\n"
        "session:\n"
        "  timezone: America/New_York\n"
        '  open: "09:30"\n'
        '  cutoff: "09:32"\n'
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify, mff, fundednext]\n"
        "  - name: dryrun_notify\n"
        "    type: notify\n"
        "    enabled: true\n"
        "    accounts: []\n"
    )
    return config, ticks


async def test_a_stop_hit_on_the_cutoff_bar_exits_via_stop_not_flatten(tmp_path: Path) -> None:
    config, ticks = two_minute_session_settings(tmp_path)
    tight_strategy = AlwaysStrategy(stop_offset=Decimal("1"), target_offset=Decimal("1000"))

    engine = await run_from_config(
        config, ticks, "always", None, strategy_override=tight_strategy
    )

    events = journal_events(tmp_path)

    closes = [e for e in events if e["event"] == "position_closed"]
    assert len(closes) == 1
    assert closes[0]["exit_reason"] == "STOP", (
        "the position already exited via its own stop on this bar; the session "
        "manager must see that (position is None) and not also flatten it"
    )

    flattens = [
        e
        for e in events
        if e["event"] == "signal_emitted" and e["intent"] == SignalIntent.FLATTEN.value
    ]
    assert not flattens, "a position that already exited this bar must not also be flattened"

    results = [e for e in events if e["event"] == "order_result"]
    assert len(results) == 4, "only the entry is ever dispatched; a stop/target exit is local"
    assert engine.trades_taken == 1



# --- Required additions: pin C1/C2/C3 directly (see docs/superpowers/sdd
# review of the state-and-recovery section). The three tests above already
# exercise the fix end to end; these three isolate each finding precisely
# enough that reverting just its part of the fix fails only the matching
# test here. Verified by mutation: reintroducing the old predicate
# (`warmup = resume_from is not None and bar.close_time <= resume_from`,
# with no separate skip window, and `PositionTracker.on_bar`/
# `strategy.on_bar` called unconditionally every bar) makes all three fail;
# with the fix in place, all three pass.


async def test_accumulating_strategy_crash_resume_matches_an_uninterrupted_run(
    tmp_path: Path,
) -> None:
    """Pins C1: bars already reflected in restored state must not be replayed
    through strategy.on_bar. AlwaysStrategy's `{"fired": bool}` state cannot
    catch this -- it is idempotent, so a bar replayed on top of an already-
    correct restore still looks correct afterward. AccumulatingStrategy
    cannot hide it: bars_seen and sum_close only ever grow, so any bar
    counted twice is permanent and visible in the final totals.
    """
    uninterrupted_dir = tmp_path / "uninterrupted"
    uninterrupted_dir.mkdir()
    uninterrupted = await run_from_config(
        settings_for(uninterrupted_dir),
        FIXTURE,
        "stub",
        None,
        strategy_override=AccumulatingStrategy(),
    )

    resumed_dir = tmp_path / "resumed"
    resumed_dir.mkdir()
    resumed_config = settings_for(resumed_dir)
    await run_from_config(
        resumed_config, FIXTURE, "stub", max_bars=30, strategy_override=AccumulatingStrategy()
    )
    resumed = await run_from_config(
        resumed_config, FIXTURE, "stub", max_bars=None, strategy_override=AccumulatingStrategy()
    )

    assert isinstance(uninterrupted.strategy, AccumulatingStrategy)
    assert isinstance(resumed.strategy, AccumulatingStrategy)
    assert uninterrupted.strategy.bars_seen > 0, "precondition: the fixture has 1m bars"
    assert resumed.strategy.bars_seen == uninterrupted.strategy.bars_seen, (
        "a crashed-and-resumed run must see exactly the same bars as an "
        "uninterrupted one -- not the pre-crash bars twice"
    )
    assert resumed.strategy.sum_close == uninterrupted.strategy.sum_close


async def test_strategy_on_bar_is_not_called_for_bars_at_or_before_resume_from(
    tmp_path: Path,
) -> None:
    """Pins C1 directly: asserts on a call counter, as specified."""
    config = settings_for(tmp_path)
    await run_from_config(
        config, FIXTURE, "stub", max_bars=15, strategy_override=CallCountingStrategy()
    )

    store = StateStore(load_settings(config).state_db_path)
    await store.init_schema()
    state = await store.load(SESSION)
    assert state is not None and state.last_bar_time is not None
    resume_from = state.last_bar_time

    counting = CallCountingStrategy()
    await run_from_config(config, FIXTURE, "stub", max_bars=None, strategy_override=counting)

    assert counting.calls, "precondition: the live tail must still call on_bar at least once"
    assert all(close_time > resume_from for close_time in counting.calls), (
        "strategy.on_bar must not be called for any bar at or before resume_from"
    )


def delayed_entry_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Five clean 1m bars, 1m timeframe only. Bar 1 dips to a low of 50 then
    every later bar holds flat at 100.

    Built for
    test_a_restored_position_is_not_closed_by_bars_predating_its_own_entry,
    which needs a bar that exists (and whose range would trip a stop-loss if
    misevaluated) before the position it must not be tested against even
    exists yet.
    """
    ticks = tmp_path / "ticks.jsonl"
    rows = [
        ("2026-07-15T13:30:00+00:00", "100.00"),
        ("2026-07-15T13:30:30+00:00", "50.00"),
        ("2026-07-15T13:31:00+00:00", "100.00"),
        ("2026-07-15T13:31:30+00:00", "100.00"),
        ("2026-07-15T13:32:00+00:00", "100.00"),
        ("2026-07-15T13:32:30+00:00", "100.00"),
        ("2026-07-15T13:33:00+00:00", "100.00"),
        ("2026-07-15T13:33:30+00:00", "100.00"),
        ("2026-07-15T13:34:00+00:00", "100.00"),
        ("2026-07-15T13:34:30+00:00", "100.00"),
        ("2026-07-15T13:35:00+00:00", "100.00"),
    ]
    ticks.write_text(
        "\n".join(json.dumps({"ts": ts, "price": price, "size": 1}) for ts, price in rows)
        + "\n"
    )

    config = tmp_path / "test.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m]\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify, mff, fundednext]\n"
        "  - name: dryrun_notify\n"
        "    type: notify\n"
        "    enabled: true\n"
        "    accounts: []\n"
    )
    return config, ticks


async def test_a_restored_position_is_not_closed_by_bars_predating_its_own_entry(
    tmp_path: Path,
) -> None:
    """Pins C3.

    Bar 1 (low 50) closes before the position (opened on bar 2: entry 100,
    stop 60) exists. On an uninterrupted run there is no position yet for
    PositionTracker.on_bar to test bar 1 against. On a resumed run,
    PositionTracker.restore puts the position back before bar 1 is replayed
    -- if bar 1 is fed through on_bar again, its low of 50 fabricates a
    stop-out 90 seconds before the position it is supposedly closing was
    even opened.
    """
    config, ticks = delayed_entry_fixture(tmp_path)

    first = await run_from_config(
        config,
        ticks,
        "always",
        max_bars=4,
        strategy_override=DelayedEntryStrategy(
            entry_after=2, stop_offset=Decimal("40"), target_offset=Decimal("1000")
        ),
    )
    assert first.trades_taken == 1

    second = await run_from_config(
        config,
        ticks,
        "always",
        max_bars=5,
        strategy_override=DelayedEntryStrategy(
            entry_after=2, stop_offset=Decimal("40"), target_offset=Decimal("1000")
        ),
    )
    assert second.trades_taken == 1

    store = StateStore(load_settings(config).state_db_path)
    await store.init_schema()
    state = await store.load(SESSION)
    assert state is not None
    assert state.position is not None, "the position must survive the resume, not be stopped out"
    assert state.position.entry_price == Decimal("100.00")
    assert state.position.stop_price == Decimal("60.00")

    closes = [e for e in journal_events(tmp_path) if e["event"] == "position_closed"]
    assert not closes, "no bar in this fixture should ever close the position"



# --- Required addition: I1 -- trades_taken and is_halted must reset on a
# session rollover. SessionManager owns the day boundary; these counters
# live on Engine, and nothing connected the two before this fix.


def two_session_fixture(
    tmp_path: Path, bars_per_day: int, max_trades_per_day: int = 2
) -> tuple[Path, Path]:
    """Two consecutive trading days (2026-07-15, 2026-07-16), each with
    `bars_per_day` clean 1m bars starting at 09:30 America/New_York, all
    within a single continuous run (no crash, no resume_from).
    """
    ticks_path = tmp_path / "ticks.jsonl"
    lines: list[str] = []
    for day in (date(2026, 7, 15), date(2026, 7, 16)):
        session_open = datetime.combine(day, time(13, 30), tzinfo=timezone.utc)
        for minute in range(bars_per_day + 1):  # +1 tick to close the final bar
            ts = session_open + timedelta(minutes=minute)
            lines.append(json.dumps({"ts": ts.isoformat(), "price": "100.00", "size": 1}))
    ticks_path.write_text("\n".join(lines) + "\n")

    config = tmp_path / "test.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m]\n"
        "risk:\n"
        f"  max_trades_per_day: {max_trades_per_day}\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify, mff, fundednext]\n"
        "  - name: dryrun_notify\n"
        "    type: notify\n"
        "    enabled: true\n"
        "    accounts: []\n"
    )
    return config, ticks_path


async def test_trades_taken_resets_on_a_session_rollover(tmp_path: Path) -> None:
    """Pins I1's trades_taken half.

    AlwaysStrategy re-arms every session -- on_session_start resets its
    `_fired` flag -- so it attempts exactly one entry on day 1 and another on
    day 2 of this single continuous two-day run. With max_trades_per_day=1,
    day 2's attempt is only accepted if trades_taken reset to 0 at the
    rollover; without the reset, day 1's single trade permanently pins the
    counter at the daily cap and day 2 is vetoed MAX_TRADES -- measured
    behaviour: "day 2 emitted zero trades, vetoed MAX_TRADES."
    """
    config, ticks = two_session_fixture(tmp_path, bars_per_day=3, max_trades_per_day=1)

    await run_from_config(config, ticks, "always", None, strategy_override=AlwaysStrategy())

    def entries(day: date) -> list[dict[str, object]]:
        return [
            e
            for e in journal_events_for(tmp_path, day)
            if e["event"] == "signal_emitted" and e["intent"] == SignalIntent.ENTRY.value
        ]

    day1_entries = entries(date(2026, 7, 15))
    day2_entries = entries(date(2026, 7, 16))
    day2_vetoes = [
        e for e in journal_events_for(tmp_path, date(2026, 7, 16)) if e["event"] == "risk_veto"
    ]

    assert len(day1_entries) == 1, "precondition: day 1 must actually take its one trade"
    assert not day2_vetoes, f"day 2 must not veto on a stale trade count: {day2_vetoes}"
    assert len(day2_entries) == 1, (
        "day 2's entry must not be blocked by day 1's trade count -- "
        "max_trades_per_day is a daily limit, not a per-process one"
    )


async def test_is_halted_resets_on_a_session_rollover(tmp_path: Path) -> None:
    """Pins I1's is_halted half.

    ExplodingStrategy raises once, on its third on_bar call ever (a running
    count, not a per-session one), which lands on day 1's third and final
    bar here. Without a reset, is_halted stays True for the rest of the
    process -- day 2 never gets another on_bar call, even though the
    strategy would run cleanly from here on -- measured behaviour: "a
    strategy that raised on Monday stays halted forever."
    """
    config, ticks = two_session_fixture(tmp_path, bars_per_day=3)

    engine = await run_from_config(
        config, ticks, "stub", None, strategy_override=ExplodingStrategy()
    )

    day1_errors = [
        e
        for e in journal_events_for(tmp_path, date(2026, 7, 15))
        if e["event"] == "strategy_error"
    ]
    assert day1_errors, "precondition: the strategy must actually raise on day 1"
    assert engine.is_halted is False, "a halt from a prior session must not persist into a new one"


async def test_is_halted_is_restored_on_resume(tmp_path: Path) -> None:
    """The other half of the is_halted finding: run_from_config previously
    never read prior.is_halted back, and Engine.__init__ hardcoded False, so
    a halted strategy silently came back up un-halted after every restart --
    the crash the halt exists to protect against would just recur.
    """
    config = settings_for(tmp_path)
    first = await run_from_config(
        settings_for(tmp_path), FIXTURE, "stub", max_bars=5, strategy_override=ExplodingStrategy()
    )
    assert first.is_halted is True, "precondition: the strategy must actually raise"

    second = await run_from_config(
        config, FIXTURE, "stub", max_bars=6, strategy_override=ExplodingStrategy()
    )
    assert second.is_halted is True, "is_halted must be restored from the persisted state"

# --- Required addition: I2 -- the tracker's refusal must be observable.


async def test_a_refused_second_entry_is_journaled_and_not_counted_as_a_trade(
    tmp_path: Path,
) -> None:
    """Pins I2.

    LONG then SHORT, one bar apart: both clear risk and both dispatch (the
    router and risk layer have no notion of a locally tracked position), but
    PositionTracker.on_signal silently refused the SHORT before this fix --
    trades_taken still counted it, and the journal recorded a second
    position_opened that never actually happened locally. Measured
    behaviour: "two position_opened records, two broker dispatches,
    trades_taken=2, and one actual tracked position ... The broker received
    a reversal." max_bars=5 keeps the run well clear of the session cutoff,
    so the only signals in play are the two entries -- no cutoff flatten to
    add its own order_result records to the count.
    """
    config = settings_for(tmp_path)
    engine = await run_from_config(
        config, FIXTURE, "always", max_bars=5, strategy_override=LongThenShortStrategy()
    )

    events = journal_events(tmp_path)
    opened = [e for e in events if e["event"] == "position_opened"]
    rejected = [e for e in events if e["event"] == "position_open_rejected"]
    results = [e for e in events if e["event"] == "order_result"]

    assert len(opened) == 1, "only the first entry actually opens a local position"
    assert len(rejected) == 1, "the second entry's refusal must be journaled, not swallowed"
    assert rejected[0]["reason"] == "position already open"
    assert len(results) == 8, "both signals reach the router regardless of the local refusal"
    assert engine.trades_taken == 1, "a refused entry must not be counted as a trade"

    store = StateStore(load_settings(config).state_db_path)
    await store.init_schema()
    state = await store.load(SESSION)
    assert state is not None
    assert state.trades_taken == 1
    assert state.position is not None
    assert state.position.direction is Direction.LONG, (
        "the tracked position must still be the first entry, not silently replaced"
    )
