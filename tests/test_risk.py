from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from nq_agent.clock import SessionCalendar
from nq_agent.models import Direction, Signal, SignalIntent, VetoReason
from nq_agent.risk.accounts import AccountRegistry
from nq_agent.risk.limits import RiskManager

IN_SESSION = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)  # 10:00 New York
AFTER_CUTOFF = datetime(2026, 7, 15, 20, 45, tzinfo=timezone.utc)  # 16:45 New York
BEFORE_OPEN = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)  # 08:00 New York


def calendar() -> SessionCalendar:
    return SessionCalendar("America/New_York", time(9, 30), time(16, 30))


def manager(kill_switch: Path, max_trades: int = 2, window: int = 60) -> RiskManager:
    return RiskManager(
        calendar=calendar(),
        max_trades_per_day=max_trades,
        duplicate_window_seconds=window,
        kill_switch_path=kill_switch,
    )


def entry(at: datetime = IN_SESSION, direction: Direction = Direction.LONG) -> Signal:
    long_side = direction is Direction.LONG
    return Signal(
        timestamp=at,
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=direction,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090") if long_side else Decimal("20110"),
        target_price=Decimal("20120") if long_side else Decimal("20080"),
        quantity=1,
        reason="test",
    )


def flatten(at: datetime = AFTER_CUTOFF) -> Signal:
    return Signal(
        timestamp=at,
        symbol="NQ",
        intent=SignalIntent.FLATTEN,
        direction=Direction.LONG,
        quantity=1,
        reason="session cutoff",
    )


def test_clean_entry_passes(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt")
    assert risk.check(entry(), trades_taken=0, enabled_executor_count=3) is None


def test_max_trades_vetoes(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt", max_trades=2)
    veto = risk.check(entry(), trades_taken=2, enabled_executor_count=3)
    assert veto is not None
    assert veto.reason is VetoReason.MAX_TRADES


def test_past_cutoff_vetoes(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt")
    veto = risk.check(entry(at=AFTER_CUTOFF), trades_taken=0, enabled_executor_count=3)
    assert veto is not None
    assert veto.reason is VetoReason.PAST_CUTOFF


def test_before_open_vetoes_as_session_closed(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt")
    veto = risk.check(entry(at=BEFORE_OPEN), trades_taken=0, enabled_executor_count=3)
    assert veto is not None
    assert veto.reason is VetoReason.SESSION_CLOSED


def test_kill_switch_file_vetoes_and_is_not_cached(tmp_path: Path) -> None:
    halt = tmp_path / "halt"
    risk = manager(halt)
    assert risk.check(entry(), trades_taken=0, enabled_executor_count=3) is None

    halt.write_text("halt")
    veto = risk.check(entry(), trades_taken=0, enabled_executor_count=3)
    assert veto is not None
    assert veto.reason is VetoReason.KILL_SWITCH

    halt.unlink()
    assert risk.check(entry(), trades_taken=0, enabled_executor_count=3) is None


def test_no_enabled_executors_vetoes(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt")
    veto = risk.check(entry(), trades_taken=0, enabled_executor_count=0)
    assert veto is not None
    assert veto.reason is VetoReason.ACCOUNT_DISABLED


def test_duplicate_within_window_is_rejected(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt", window=60)
    first = entry()
    assert risk.check(first, 0, 3) is None
    risk.record_accepted(first)

    second = entry(at=IN_SESSION + timedelta(seconds=30))
    veto = risk.check(second, 1, 3)
    assert veto is not None
    assert veto.reason is VetoReason.DUPLICATE_SIGNAL


def test_duplicate_outside_window_is_allowed(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt", window=60)
    first = entry()
    risk.record_accepted(first)
    later = entry(at=IN_SESSION + timedelta(seconds=61))
    assert risk.check(later, 1, 3) is None


def test_opposite_direction_within_window_is_allowed(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt", window=60)
    risk.record_accepted(entry())
    other = entry(at=IN_SESSION + timedelta(seconds=10), direction=Direction.SHORT)
    assert risk.check(other, 1, 3) is None


def test_flatten_bypasses_every_check(tmp_path: Path) -> None:
    halt = tmp_path / "halt"
    halt.write_text("halt")
    risk = manager(halt, max_trades=0)
    assert risk.check(flatten(), trades_taken=99, enabled_executor_count=0) is None


def test_flatten_bypasses_even_a_pending_duplicate(tmp_path: Path) -> None:
    """test_flatten_bypasses_every_check combines kill switch + past cutoff + max
    trades + zero executors, but never a pending duplicate, since the duplicate
    check only runs last, after the FLATTEN short-circuit. Confirmed by mutation
    that this gap is real: reordering the duplicate check ahead of the FLATTEN
    bypass in RiskManager.check leaves all of the tests above green, because none
    of them record a matching entry before checking a FLATTEN signal. This test
    combines all five veto conditions at once, so any reordering of the FLATTEN
    bypass relative to any single check - not just the kill switch - fails it.
    """
    halt = tmp_path / "halt"
    halt.write_text("halt")
    risk = manager(halt, max_trades=0, window=60)
    risk.record_accepted(entry(at=AFTER_CUTOFF - timedelta(seconds=10)))
    assert risk.check(flatten(), trades_taken=99, enabled_executor_count=0) is None


def test_account_registry_rereads_the_file(tmp_path: Path) -> None:
    config = tmp_path / "accounts.yaml"
    config.write_text("tradeify: true\nmff: true\nfundednext: false\n")
    registry = AccountRegistry(config)
    assert registry.enabled_accounts() == {"tradeify", "mff"}

    config.write_text("tradeify: false\nmff: true\nfundednext: true\n")
    assert registry.enabled_accounts() == {"mff", "fundednext"}


def test_missing_account_file_returns_none_meaning_no_overrides(tmp_path: Path) -> None:
    registry = AccountRegistry(tmp_path / "absent.yaml")
    assert registry.enabled_accounts() is None


def test_account_registry_rejects_a_quoted_boolean(tmp_path: Path) -> None:
    """"false" is a truthy string; a truthiness cast would enable the account."""
    config = tmp_path / "accounts.yaml"
    config.write_text('tradeify: true\nfundednext: "false"\n')

    with pytest.raises(ValueError, match="must be true or false"):
        AccountRegistry(config).enabled_accounts()


def test_account_registry_accepts_yaml_boolean_idioms(tmp_path: Path) -> None:
    config = tmp_path / "accounts.yaml"
    config.write_text("tradeify: yes\nmff: no\nfundednext: off\ntopstep: on\n")
    assert AccountRegistry(config).enabled_accounts() == {"tradeify", "topstep"}


def test_account_registry_rejects_a_non_mapping(tmp_path: Path) -> None:
    config = tmp_path / "accounts.yaml"
    config.write_text("- tradeify\n- mff\n")

    with pytest.raises(ValueError, match="must contain a mapping"):
        AccountRegistry(config).enabled_accounts()


def test_empty_account_file_disables_everything(tmp_path: Path) -> None:
    """Deliberately asymmetric with a missing file: truncation must fail closed."""
    config = tmp_path / "accounts.yaml"
    config.write_text("")
    assert AccountRegistry(config).enabled_accounts() == set()


def test_recent_signals_are_pruned_without_an_intervening_check(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt", window=60)
    for offset in range(0, 600, 10):
        risk.record_accepted(entry(at=IN_SESSION + timedelta(seconds=offset)))

    assert len(risk._recent) <= 7
