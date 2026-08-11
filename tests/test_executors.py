from datetime import datetime, timezone
from decimal import Decimal

from nq_agent.execution.dryrun import DryRunExecutor, DryRunNotifier
from nq_agent.models import Direction, Signal, SignalIntent


def entry_signal() -> Signal:
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


async def test_dry_run_executor_reports_success_with_its_account() -> None:
    executor = DryRunExecutor("dryrun", account_id="tradeify")
    result = await executor.execute(entry_signal())

    assert result.success is True
    assert result.executor_name == "dryrun:tradeify"
    assert result.account_id == "tradeify"
    assert result.error is None


async def test_dry_run_executor_records_what_it_was_asked_to_send() -> None:
    executor = DryRunExecutor("dryrun", account_id="mff")
    signal = entry_signal()
    await executor.execute(signal)
    assert executor.sent == [signal]


async def test_dry_run_executor_echoes_the_signal_in_raw_response() -> None:
    executor = DryRunExecutor("dryrun", account_id="mff")
    signal = entry_signal()
    result = await executor.execute(signal)
    assert result.raw_response["intent"] == "ENTRY"
    assert result.raw_response["direction"] == "LONG"
    assert result.raw_response["entry_price"] == "20100"


async def test_dry_run_notifier_has_no_account_and_records_alerts() -> None:
    notifier = DryRunNotifier("notify")
    result = await notifier.execute(entry_signal())
    assert result.executor_name == "notify"
    assert result.account_id is None

    await notifier.alert("executor signaltradeapp:mff failed")
    assert notifier.alerts == ["executor signaltradeapp:mff failed"]


async def test_health_check_passes_for_dry_run_components() -> None:
    assert await DryRunExecutor("dryrun", account_id="tradeify").health_check() is True
    assert await DryRunNotifier("notify").health_check() is True


async def test_empty_account_id_is_still_suffixed_into_the_name() -> None:
    """Truthiness would drop the suffix while account_id still stored ""."""
    executor = DryRunExecutor("dryrun", account_id="")
    assert executor.name == "dryrun:"
    assert executor.account_id == ""


async def test_flatten_signal_carries_no_price_keys() -> None:
    signal = Signal(
        timestamp=datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc),
        symbol="NQ",
        intent=SignalIntent.FLATTEN,
        direction=Direction.LONG,
        quantity=2,
        reason="session cutoff",
    )
    result = await DryRunExecutor("dryrun", account_id="tradeify").execute(signal)

    assert result.success is True
    assert result.raw_response["intent"] == "FLATTEN"
    assert result.raw_response["direction"] == "LONG"
    assert result.raw_response["quantity"] == "2"
    assert "entry_price" not in result.raw_response
    assert "stop_price" not in result.raw_response
    assert "target_price" not in result.raw_response
