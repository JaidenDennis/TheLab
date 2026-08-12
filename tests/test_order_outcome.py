"""`success: bool` cannot express "I do not know".

A timeout, a dropped connection mid-POST, or a cancellation between sending
the order and reading the response all leave the same question open: did the
broker fill it? Reporting that as success=False says it definitely did not,
which is the one answer that is certainly wrong -- and the recovery action for
"rejected" (retry, or trade elsewhere) is the exact opposite of the one for
"unknown" (reconcile before doing anything).
"""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SimClock
from nq_agent.execution.base import Executor
from nq_agent.journal import Journal
from nq_agent.models import Direction, OrderOutcome, OrderResult, Signal, SignalIntent
from nq_agent.router import Router

SESSION = date(2026, 7, 15)


def signal() -> Signal:
    return Signal(
        timestamp=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.LONG,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
        quantity=1,
        reason="test",
    )


def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path, SimClock(datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc)))


class HangingExecutor(Executor):
    """Never answers -- the shape of a broker that accepted the order and then
    stopped talking."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.account_id = name
        self.enabled = True

    async def execute(self, sig: Signal) -> OrderResult:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    async def health_check(self) -> bool:
        return True


class RejectingExecutor(Executor):
    """Answers, and the answer is no. Definitely not filled."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.account_id = name
        self.enabled = True

    async def execute(self, sig: Signal) -> OrderResult:
        return OrderResult(
            signal_id=sig.id,
            executor_name=self.name,
            outcome=OrderOutcome.REJECTED,
            error="insufficient margin",
        )

    async def health_check(self) -> bool:
        return True


def test_filled_is_the_only_outcome_that_counts_as_success() -> None:
    for outcome, expected in (
        (OrderOutcome.FILLED, True),
        (OrderOutcome.REJECTED, False),
        (OrderOutcome.UNKNOWN, False),
    ):
        result = OrderResult(signal_id="s", executor_name="e", outcome=outcome)
        assert result.success is expected


def test_unknown_is_not_the_same_as_rejected() -> None:
    """The whole point. Both are 'not confirmed filled', and they call for
    opposite recovery."""
    unknown = OrderResult(signal_id="s", executor_name="e", outcome=OrderOutcome.UNKNOWN)
    rejected = OrderResult(signal_id="s", executor_name="e", outcome=OrderOutcome.REJECTED)

    assert unknown.success == rejected.success
    assert unknown.outcome is not rejected.outcome
    assert unknown.needs_reconciliation is True
    assert rejected.needs_reconciliation is False


async def test_a_timeout_is_unknown_not_rejected(tmp_path: Path) -> None:
    """A broker that has not answered inside the timeout may still be filling
    the order. Calling that a rejection invents a fact."""
    router = Router([HangingExecutor("slow")], journal(tmp_path), 0.05, 0.05, "continue")

    results = await router.dispatch(signal(), SESSION)

    assert results[0].outcome is OrderOutcome.UNKNOWN
    assert results[0].error == "timeout"


async def test_an_executor_that_raises_is_unknown(tmp_path: Path) -> None:
    """An exception from inside execute() is ambiguous: it may have been
    raised before the request went out, or after the broker received it."""

    class ExplodingExecutor(Executor):
        def __init__(self) -> None:
            self.name = "boom"
            self.account_id = "boom"
            self.enabled = True

        async def execute(self, sig: Signal) -> OrderResult:
            raise ConnectionResetError("connection reset mid-POST")

        async def health_check(self) -> bool:
            return True

    router = Router([ExplodingExecutor()], journal(tmp_path), 1.0, 1.0, "continue")

    results = await router.dispatch(signal(), SESSION)

    assert results[0].outcome is OrderOutcome.UNKNOWN


async def test_an_explicit_rejection_stays_rejected(tmp_path: Path) -> None:
    """A broker that answered 'no' is not ambiguous and must not be widened
    into UNKNOWN -- that would send the operator reconciling a fill that
    certainly did not happen."""
    router = Router([RejectingExecutor("no")], journal(tmp_path), 1.0, 1.0, "continue")

    results = await router.dispatch(signal(), SESSION)

    assert results[0].outcome is OrderOutcome.REJECTED


async def test_the_journal_records_the_outcome_not_just_a_boolean(
    tmp_path: Path,
) -> None:
    """The journal is what an operator reads at 3pm to work out what the agent
    thinks it holds. A bare success flag cannot tell them to go and check."""
    import json

    router = Router([HangingExecutor("slow")], journal(tmp_path), 0.05, 0.05, "continue")
    await router.dispatch(signal(), SESSION)

    records = [
        json.loads(line)
        for line in (tmp_path / "2026-07-15.jsonl").read_text().strip().splitlines()
    ]
    order_results = [r for r in records if r["event"] == "order_result"]

    assert order_results[0]["outcome"] == "UNKNOWN"


async def test_an_unknown_leg_triggers_the_partial_fan_alert(tmp_path: Path) -> None:
    """alert_only must treat 'might have filled' as at least as alarming as a
    clean rejection."""
    from nq_agent.execution.dryrun import DryRunNotifier

    notifier = DryRunNotifier("notify")
    router = Router(
        [notifier, HangingExecutor("slow")], journal(tmp_path), 0.05, 0.05, "alert_only"
    )

    await router.dispatch(signal(), SESSION)

    assert notifier.alerts, "no alert was raised for an unknown-outcome leg"
    assert "slow" in notifier.alerts[0]
