import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from nq_agent.clock import SimClock
from nq_agent.execution.base import Executor
from nq_agent.execution.dryrun import DryRunExecutor, DryRunNotifier
from nq_agent.journal import Journal
from nq_agent.models import Direction, OrderResult, Signal, SignalIntent
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


class SlowExecutor(Executor):
    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.account_id = name
        self.enabled = True
        self._delay = delay

    async def execute(self, sig: Signal) -> OrderResult:
        await asyncio.sleep(self._delay)
        return OrderResult(signal_id=sig.id, executor_name=self.name, success=True)

    async def health_check(self) -> bool:
        return True


class ExplodingExecutor(Executor):
    def __init__(self, name: str) -> None:
        self.name = name
        self.account_id = name
        self.enabled = True

    async def execute(self, sig: Signal) -> OrderResult:
        raise RuntimeError("broker exploded")

    async def health_check(self) -> bool:
        return False


class OrderRecordingNotifier(DryRunNotifier):
    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__(name)
        self._log = log

    async def execute(self, sig: Signal) -> OrderResult:
        await asyncio.sleep(0.02)
        self._log.append("notify")
        return await super().execute(sig)


class OrderRecordingExecutor(DryRunExecutor):
    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__(name, account_id="acct")
        self._log = log

    async def execute(self, sig: Signal) -> OrderResult:
        self._log.append("broker")
        return await super().execute(sig)


async def test_every_enabled_executor_receives_the_signal(tmp_path: Path) -> None:
    a = DryRunExecutor("broker", account_id="tradeify")
    b = DryRunExecutor("broker", account_id="mff")
    router = Router([a, b], journal(tmp_path), 1.0, 1.0, "continue")

    results = await router.dispatch(signal(), SESSION)

    assert {r.executor_name for r in results} == {"broker:tradeify", "broker:mff"}
    assert all(r.success for r in results)


async def test_disabled_executors_are_skipped(tmp_path: Path) -> None:
    live = DryRunExecutor("broker", account_id="tradeify")
    dark = DryRunExecutor("broker", account_id="mff", enabled=False)
    router = Router([live, dark], journal(tmp_path), 1.0, 1.0, "continue")

    results = await router.dispatch(signal(), SESSION)

    assert [r.executor_name for r in results] == ["broker:tradeify"]
    assert dark.sent == []


async def test_notify_runs_before_the_rest(tmp_path: Path) -> None:
    order: list[str] = []
    notifier = OrderRecordingNotifier("notify", order)
    broker = OrderRecordingExecutor("broker", order)
    router = Router([broker, notifier], journal(tmp_path), 1.0, 1.0, "continue")

    await router.dispatch(signal(), SESSION)

    assert order == ["notify", "broker"]


async def test_a_timeout_becomes_a_failed_result_without_blocking_others(
    tmp_path: Path,
) -> None:
    slow = SlowExecutor("slow", delay=5.0)
    fast = DryRunExecutor("broker", account_id="tradeify")
    router = Router([slow, fast], journal(tmp_path), executor_timeout=0.05,
                    notify_timeout=1.0, partial_fan="continue")

    results = await router.dispatch(signal(), SESSION)

    by_name = {r.executor_name: r for r in results}
    assert by_name["slow"].success is False
    assert by_name["slow"].error == "timeout"
    assert by_name["broker:tradeify"].success is True


async def test_an_exception_becomes_a_failed_result(tmp_path: Path) -> None:
    router = Router(
        [ExplodingExecutor("boom"), DryRunExecutor("broker", account_id="mff")],
        journal(tmp_path),
        1.0,
        1.0,
        "continue",
    )

    results = await router.dispatch(signal(), SESSION)

    by_name = {r.executor_name: r for r in results}
    assert by_name["boom"].success is False
    assert "broker exploded" in (by_name["boom"].error or "")
    assert by_name["broker:mff"].success is True


async def test_continue_mode_does_not_alert(tmp_path: Path) -> None:
    notifier = DryRunNotifier("notify")
    router = Router(
        [ExplodingExecutor("boom"), notifier], journal(tmp_path), 1.0, 1.0, "continue"
    )

    await router.dispatch(signal(), SESSION)

    assert notifier.alerts == []


async def test_alert_only_mode_alerts_on_partial_failure(tmp_path: Path) -> None:
    notifier = DryRunNotifier("notify")
    router = Router(
        [ExplodingExecutor("boom"), notifier], journal(tmp_path), 1.0, 1.0, "alert_only"
    )

    await router.dispatch(signal(), SESSION)

    assert len(notifier.alerts) == 1
    assert "boom" in notifier.alerts[0]


async def test_alert_only_mode_stays_quiet_when_everything_succeeds(tmp_path: Path) -> None:
    notifier = DryRunNotifier("notify")
    router = Router(
        [DryRunExecutor("broker", account_id="mff"), notifier],
        journal(tmp_path),
        1.0,
        1.0,
        "alert_only",
    )

    await router.dispatch(signal(), SESSION)

    assert notifier.alerts == []


async def test_every_result_is_journaled_including_failures(tmp_path: Path) -> None:
    router = Router(
        [ExplodingExecutor("boom"), DryRunExecutor("broker", account_id="mff")],
        journal(tmp_path),
        1.0,
        1.0,
        "continue",
    )

    await router.dispatch(signal(), SESSION)

    lines = (tmp_path / "2026-07-15.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert all('"event": "order_result"' in line for line in lines)


async def test_latency_is_recorded(tmp_path: Path) -> None:
    router = Router([SlowExecutor("slow", delay=0.05)], journal(tmp_path), 1.0, 1.0, "continue")
    results = await router.dispatch(signal(), SESSION)
    assert results[0].latency_ms >= 40


async def test_account_registry_filters_enabled_executors(tmp_path: Path) -> None:
    allowed: set[str] | None = {"tradeify"}
    router = Router(
        [
            DryRunExecutor("broker", account_id="tradeify"),
            DryRunExecutor("broker", account_id="mff"),
            DryRunNotifier("notify"),
        ],
        journal(tmp_path),
        1.0,
        1.0,
        "continue",
        enabled_accounts=lambda: allowed,
    )

    results = await router.dispatch(signal(), SESSION)
    assert {r.executor_name for r in results} == {"broker:tradeify", "notify"}

    allowed = {"tradeify", "mff"}
    results = await router.dispatch(signal(), SESSION)
    assert {r.executor_name for r in results} == {"broker:tradeify", "broker:mff", "notify"}


async def test_no_registry_means_every_account_is_enabled(tmp_path: Path) -> None:
    router = Router(
        [DryRunExecutor("broker", account_id="tradeify")],
        journal(tmp_path),
        1.0,
        1.0,
        "continue",
        enabled_accounts=lambda: None,
    )
    assert len(router.enabled) == 1


def test_duplicate_executor_names_are_rejected(tmp_path: Path) -> None:
    # DryRunExecutor(account_id=None) and DryRunNotifier(name) both reduce to
    # the bare name, so two same-named instances would be indistinguishable
    # in OrderResult.executor_name, the only field identifying which
    # destination produced a result. The router owns the whole instance list,
    # so it is the right place to catch this at construction time.
    with pytest.raises(ValueError, match="dup"):
        Router(
            [DryRunExecutor("dup"), DryRunNotifier("dup")],
            journal(tmp_path),
            1.0,
            1.0,
            "continue",
        )


class RendezvousExecutor(Executor):
    """Succeeds only if every sibling reaches execute() before any returns.

    A timing assertion would flake. This deadlocks instead: if the router runs
    brokers sequentially, the first one waits for an arrival count that cannot
    grow, its wait_for expires, and the router reports a timeout. Concurrency
    is therefore the only way for all of these to come back successful.
    """

    arrived = 0
    all_here = None  # type: ignore[var-annotated]

    def __init__(self, name: str, expected: int) -> None:
        self.name = name
        self.account_id = name
        self.enabled = True
        self._expected = expected

    async def execute(self, sig: Signal) -> OrderResult:
        RendezvousExecutor.arrived += 1
        assert RendezvousExecutor.all_here is not None
        if RendezvousExecutor.arrived == self._expected:
            RendezvousExecutor.all_here.set()
        await asyncio.wait_for(RendezvousExecutor.all_here.wait(), timeout=2.0)
        return OrderResult(signal_id=sig.id, executor_name=self.name, success=True)

    async def health_check(self) -> bool:
        return True


async def test_brokers_run_concurrently_not_sequentially(tmp_path: Path) -> None:
    RendezvousExecutor.arrived = 0
    RendezvousExecutor.all_here = asyncio.Event()
    brokers = [RendezvousExecutor(f"broker{i}", expected=4) for i in range(4)]

    router = Router(brokers, journal(tmp_path), 5.0, 5.0, "continue")
    results = await router.dispatch(signal(), SESSION)

    assert len(results) == 4
    assert all(r.success for r in results), (
        "a sequential fan-out cannot satisfy the rendezvous; "
        f"got {[(r.executor_name, r.error) for r in results]}"
    )
    assert RendezvousExecutor.arrived == 4
