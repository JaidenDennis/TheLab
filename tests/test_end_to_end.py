import json
from datetime import date
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


async def test_backfill_signals_are_suppressed_and_journaled(tmp_path: Path) -> None:
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
    assert suppressed, "backfill replay must journal every signal it swallowed"


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
    """Prove the warmup `return` in `_handle_signal` blocks dispatch, not just the
    journal write for it.

    The brief's own test_backfill_never_reaches_an_executor cannot tell these two
    apart by itself: with the default max_trades_per_day=2 and EveryBarStrategy
    firing on every 1m bar, the first call already pins trades_taken at the daily
    cap, so on the second call MAX_TRADES vetoes every replayed signal regardless of
    whether the warmup short-circuit is even there. Confirmed by mutation: deleting
    the `return` after the signal_suppressed_backfill journal write leaves that test
    (and every other test in this file except this one) green.

    This test raises the trade limit so risk would accept the replayed signal, so
    only the warmup check stands between it and the router. Mutation-verified: with
    the `return` deleted, this scenario leaks 4 new order_result records; with it
    restored, it leaks none.
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
