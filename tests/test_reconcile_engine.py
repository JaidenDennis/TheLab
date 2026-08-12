"""Reconciliation wired into a running engine.

The comparison logic being right is worth nothing if the engine never calls
it, never acts on the answer, or lets trading continue while the answer is
"we disagree".
"""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from nq_agent.execution.base import Executor
from nq_agent.main import order_accounts, run_from_config
from nq_agent.models import (
    BrokerPosition,
    Direction,
    OrderOutcome,
    OrderResult,
    Signal,
    SignalIntent,
    VetoReason,
)
from nq_agent.reconcile import PositionSource, StaticPositionSource

FIXTURE = Path("tests/fixtures/2026-07-15.jsonl")
SESSION = date(2026, 7, 15)


def config_for(tmp_path: Path, extra_risk: str = "") -> Path:
    config = tmp_path / "rec.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m, 5m]\n"
        "risk:\n"
        "  max_trades_per_day: 50\n"
        "  duplicate_window_seconds: 0\n" + extra_risk + "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify]\n"
    )
    return config


def events(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "journal" / f"{SESSION.isoformat()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines()]


def kinds(tmp_path: Path) -> list[str]:
    return [e["event"] for e in events(tmp_path)]


def broker_long(account: str = "tradeify", price: str = "20096.00") -> BrokerPosition:
    return BrokerPosition(
        account_id=account,
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        average_price=Decimal(price),
    )


class ExplodingSource(PositionSource):
    async def fetch_positions(self) -> list[BrokerPosition]:
        raise ConnectionError("broker API unreachable")


class UnknownExecutor(Executor):
    """Always answers UNKNOWN -- the outcome that means reconcile now."""

    def __init__(self) -> None:
        self.name = "unknown_broker"
        self.account_id = "tradeify"
        self.enabled = True

    async def execute(self, sig: Signal) -> OrderResult:
        return OrderResult(
            signal_id=sig.id,
            executor_name=self.name,
            outcome=OrderOutcome.UNKNOWN,
            account_id=self.account_id,
            error="timeout",
        )

    async def health_check(self) -> bool:
        return True


# --- the accounts that get checked -----------------------------------------


def test_order_accounts_come_from_the_executors(tmp_path: Path) -> None:
    from nq_agent.config import load_settings

    settings = load_settings(config_for(tmp_path))

    assert order_accounts(settings) == ["tradeify"]


def test_notify_entries_contribute_no_accounts(tmp_path: Path) -> None:
    from nq_agent.config import load_settings

    config = tmp_path / "n.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m]\n"
        "executors:\n"
        "  - name: notify\n"
        "    type: notify\n"
        "    enabled: true\n"
        "    accounts: []\n"
    )

    assert order_accounts(load_settings(config)) == []


# --- no source configured ---------------------------------------------------


async def test_without_a_position_source_nothing_changes(tmp_path: Path) -> None:
    """Reconciliation is opt-in. Every existing replay and backtest must run
    exactly as before."""
    engine = await run_from_config(config_for(tmp_path), FIXTURE, "always", None)

    assert engine.trades_taken == 1
    assert not [e for e in kinds(tmp_path) if e.startswith("reconciliation")]


# --- agreement --------------------------------------------------------------


async def test_agreement_is_journaled_and_does_not_block(tmp_path: Path) -> None:
    """The always strategy opens a position and the broker confirms it."""
    source = StaticPositionSource([broker_long()])

    engine = await run_from_config(
        config_for(tmp_path, "  reconcile_interval_bars: 5\n"),
        FIXTURE,
        "always",
        None,
        position_source=source,
    )

    assert "reconciliation_ok" in kinds(tmp_path)
    assert engine.trades_taken == 1


async def test_the_brokers_fill_price_replaces_the_agents(tmp_path: Path) -> None:
    """Slippage. P&L -- and therefore every money limit -- has to be measured
    against what was actually paid."""
    source = StaticPositionSource([broker_long(price="20097.50")])

    await run_from_config(
        config_for(tmp_path, "  reconcile_interval_bars: 3\n"),
        FIXTURE,
        "always",
        None,
        position_source=source,
    )

    adjusted = [e for e in events(tmp_path) if e["event"] == "reconciliation_adjusted"]
    assert adjusted, "the broker's fill price was never adopted"
    assert adjusted[0]["broker_price"] == "20097.50"

    closed = [e for e in events(tmp_path) if e["event"] == "position_closed"]
    assert closed[0]["entry_price"] == "20097.50", "P&L still used the requested price"


# --- divergence blocks trading ---------------------------------------------


async def test_a_divergence_blocks_further_entries(tmp_path: Path) -> None:
    """The broker says flat while the agent believes it holds something. No
    further entry may go out until that is resolved."""
    source = StaticPositionSource([])

    await run_from_config(
        config_for(tmp_path, "  reconcile_interval_bars: 1\n"),
        FIXTURE,
        "stub",
        None,
        strategy_override=_EveryBarStrategy(),
        position_source=source,
    )

    vetoes = [e for e in events(tmp_path) if e["event"] == "risk_veto"]
    assert any(v["reason"] == VetoReason.RECONCILIATION_REQUIRED.value for v in vetoes), (
        f"entries continued through a divergence; vetoes: {[v['reason'] for v in vetoes]}"
    )


async def test_a_phantom_position_is_dropped(tmp_path: Path) -> None:
    """Every account reports flat. The agent must stop believing it holds
    something, or it will send a flatten for a position that is not there.

    Asserted by effect, not just by the journal line: the always strategy's
    position would otherwise reach its target later in this fixture and be
    booked as a closed trade, complete with P&L for a trade that never
    happened. No position_closed at all is the proof it was really dropped.
    """
    source = StaticPositionSource([])

    await run_from_config(
        config_for(tmp_path, "  reconcile_interval_bars: 1\n"),
        FIXTURE,
        "always",
        None,
        position_source=source,
    )

    assert "reconciliation_dropped_phantom" in kinds(tmp_path)
    closes = [e for e in events(tmp_path) if e["event"] == "position_closed"]
    assert not closes, (
        "the phantom was journaled as dropped but still closed later, "
        f"booking P&L for a trade the broker never had: {closes}"
    )


async def test_a_position_the_agent_did_not_know_about_is_adopted(
    tmp_path: Path,
) -> None:
    """So the cutoff flatten can close it. Left unadopted it runs overnight."""
    source = StaticPositionSource([broker_long(price="20050.00")])

    await run_from_config(
        config_for(tmp_path, "  reconcile_interval_bars: 1\n"),
        FIXTURE,
        "stub",
        None,
        position_source=source,
    )

    adopted = [e for e in events(tmp_path) if e["event"] == "reconciliation_adopted"]
    assert adopted, "an unknown broker position was never adopted"
    assert adopted[0]["entry_price"] == "20050.00"


async def test_an_adopted_position_is_flattened_at_the_cutoff(tmp_path: Path) -> None:
    """The entire point of adopting it."""
    source = StaticPositionSource([broker_long(price="20050.00")])

    await run_from_config(
        config_for(tmp_path, "  reconcile_interval_bars: 1\n"),
        FIXTURE,
        "stub",
        None,
        position_source=source,
    )

    closed = [e for e in events(tmp_path) if e["event"] == "position_closed"]
    assert closed, "the adopted position was never closed"
    assert closed[-1]["exit_reason"] == "FLATTEN"


async def test_an_adopted_position_is_never_stopped_out(tmp_path: Path) -> None:
    """It has no stop, because the broker never told us what one would be.
    A fabricated level would exit at a price nobody chose."""
    source = StaticPositionSource([broker_long(price="20050.00")])

    await run_from_config(
        config_for(tmp_path, "  reconcile_interval_bars: 1\n"),
        FIXTURE,
        "stub",
        None,
        position_source=source,
    )

    reasons = [
        e["exit_reason"] for e in events(tmp_path) if e["event"] == "position_closed"
    ]
    assert "STOP" not in reasons
    assert "TARGET" not in reasons


# --- an UNKNOWN order triggers a check -------------------------------------


async def test_an_unknown_order_triggers_reconciliation(tmp_path: Path) -> None:
    """The trigger that matters most. The agent has just sent an order and
    does not know whether it filled."""
    import nq_agent.main as main_module

    original = main_module.build_executors
    main_module.build_executors = lambda settings: [UnknownExecutor()]  # type: ignore[assignment]
    try:
        await run_from_config(
            config_for(tmp_path),
            FIXTURE,
            "always",
            None,
            position_source=StaticPositionSource([]),
        )
    finally:
        main_module.build_executors = original  # type: ignore[assignment]

    triggers = [
        e.get("trigger") for e in events(tmp_path) if e["event"].startswith("reconciliation")
    ]
    assert "unknown_order" in triggers


# --- a failed query is itself a reason to stop -----------------------------


async def test_a_failed_broker_query_blocks_trading(tmp_path: Path) -> None:
    """Not knowing the answer carries the same risk as knowing it is wrong."""
    await run_from_config(
        config_for(tmp_path, "  reconcile_interval_bars: 1\n"),
        FIXTURE,
        "stub",
        None,
        strategy_override=_EveryBarStrategy(),
        position_source=ExplodingSource(),
    )

    assert "reconciliation_failed" in kinds(tmp_path)
    vetoes = [e for e in events(tmp_path) if e["event"] == "risk_veto"]
    assert any(v["reason"] == VetoReason.RECONCILIATION_REQUIRED.value for v in vetoes)


async def test_a_failed_query_does_not_crash_the_run(tmp_path: Path) -> None:
    engine = await run_from_config(
        config_for(tmp_path, "  reconcile_interval_bars: 1\n"),
        FIXTURE,
        "stub",
        None,
        position_source=ExplodingSource(),
    )

    assert "session_end" in kinds(tmp_path)
    assert engine is not None


# --- flatten is never blocked ----------------------------------------------


async def test_a_flatten_still_goes_out_during_a_divergence(tmp_path: Path) -> None:
    """Same rule as the kill switch: a control that traps you in a position
    you may or may not hold is worse than no control."""
    from nq_agent.clock import SessionCalendar
    from nq_agent.risk.limits import RiskManager

    risk = RiskManager(
        calendar=SessionCalendar("America/New_York", _t(9, 30), _t(16, 30)),
        max_trades_per_day=10,
        duplicate_window_seconds=0,
        kill_switch_path=tmp_path / "halt",
    )
    risk.require_reconciliation("broker disagrees")

    flatten = Signal(
        timestamp=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
        symbol="NQ",
        intent=SignalIntent.FLATTEN,
        direction=Direction.LONG,
        quantity=1,
        reason="cutoff",
    )

    assert risk.check(flatten, trades_taken=0, enabled_executor_count=1) is None


def _t(hour: int, minute: int):
    from datetime import time

    return time(hour, minute)


class _EveryBarStrategy:
    """Keeps trying to enter, so a block is observable as a veto."""

    name = "every_bar"
    required_timeframes = ["1m"]

    def on_bar(self, bar, context):  # type: ignore[no-untyped-def]
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

    def get_state(self) -> dict:
        return {}

    def restore_state(self, state: dict) -> None:
        return None
