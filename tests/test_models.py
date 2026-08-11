from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nq_agent.models import (
    Bar,
    Direction,
    OrderResult,
    Position,
    PositionClose,
    RiskVeto,
    SessionState,
    Signal,
    SignalIntent,
    Tick,
    VetoReason,
)


def _bar(**overrides: object) -> Bar:
    defaults: dict[str, object] = {
        "symbol": "NQ",
        "timeframe": "1m",
        "open_time": datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc),
        "open": Decimal("20100"),
        "high": Decimal("20110"),
        "low": Decimal("20090"),
        "close": Decimal("20105"),
        "volume": 500,
    }
    defaults.update(overrides)
    return Bar(**defaults)  # type: ignore[arg-type]


def test_bar_close_time_derives_from_timeframe() -> None:
    bar = _bar()
    assert bar.close_time == datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc)
    assert _bar(timeframe="5m").close_time == datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc)


def test_bar_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _bar(open_time=datetime(2026, 7, 15, 13, 30))


def test_bar_rejects_unknown_timeframe() -> None:
    with pytest.raises(ValidationError, match="unsupported timeframe"):
        _bar(timeframe="3m")


def test_bar_is_frozen() -> None:
    bar = _bar()
    with pytest.raises(ValidationError):
        bar.close = Decimal("1")  # type: ignore[misc]


def test_tick_normalises_to_utc_and_is_frozen() -> None:
    tick = Tick(
        symbol="NQ",
        ts=datetime(2026, 7, 15, 13, 30, 15, tzinfo=timezone.utc),
        price=Decimal("20100.25"),
        size=3,
    )
    assert tick.ts == datetime(2026, 7, 15, 13, 30, 15, tzinfo=timezone.utc)
    assert tick.price == Decimal("20100.25")
    with pytest.raises(ValidationError):
        tick.size = 9  # type: ignore[misc]


def test_tick_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Tick(
            symbol="NQ",
            ts=datetime(2026, 7, 15, 13, 30, 15),
            price=Decimal("20100.25"),
            size=3,
        )


def _entry(**overrides: object) -> Signal:
    defaults: dict[str, object] = {
        "timestamp": datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        "symbol": "NQ",
        "intent": SignalIntent.ENTRY,
        "direction": Direction.LONG,
        "entry_price": Decimal("20105"),
        "stop_price": Decimal("20095"),
        "target_price": Decimal("20125"),
        "quantity": 1,
        "reason": "test",
    }
    defaults.update(overrides)
    return Signal(**defaults)  # type: ignore[arg-type]


def test_entry_signal_gets_a_unique_id() -> None:
    assert _entry().id != _entry().id


def test_long_entry_requires_stop_below_entry_below_target() -> None:
    with pytest.raises(ValidationError, match="stop_price < entry_price < target_price"):
        _entry(stop_price=Decimal("20115"))


def test_short_entry_requires_target_below_entry_below_stop() -> None:
    signal = _entry(
        direction=Direction.SHORT,
        stop_price=Decimal("20115"),
        target_price=Decimal("20085"),
    )
    assert signal.direction is Direction.SHORT
    with pytest.raises(ValidationError, match="target_price < entry_price < stop_price"):
        _entry(direction=Direction.SHORT)


def test_entry_requires_all_three_prices() -> None:
    with pytest.raises(ValidationError, match="requires entry_price, stop_price, target_price"):
        _entry(target_price=None)


def test_entry_requires_positive_quantity() -> None:
    with pytest.raises(ValidationError):
        _entry(quantity=0)


def test_flatten_forbids_price_fields() -> None:
    with pytest.raises(ValidationError, match="must not carry price fields"):
        _entry(intent=SignalIntent.FLATTEN)


def test_flatten_signal_is_valid_without_prices() -> None:
    signal = Signal(
        timestamp=datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc),
        symbol="NQ",
        intent=SignalIntent.FLATTEN,
        direction=Direction.LONG,
        quantity=1,
        reason="session cutoff",
    )
    assert signal.entry_price is None


def test_order_result_and_veto_round_trip() -> None:
    result = OrderResult(
        signal_id="abc",
        executor_name="dryrun:tradeify",
        success=True,
        account_id="tradeify",
        latency_ms=12,
    )
    assert result.error is None
    veto = RiskVeto(signal_id="abc", reason=VetoReason.KILL_SWITCH, detail="halt file present")
    assert veto.reason is VetoReason.KILL_SWITCH


def test_position_close_carries_exit_detail() -> None:
    position = Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        entry_price=Decimal("20105"),
        entry_time=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        stop_price=Decimal("20095"),
        target_price=Decimal("20125"),
    )
    closed = PositionClose(
        position=position,
        exit_price=Decimal("20095"),
        exit_time=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
        exit_reason="STOP",
    )
    assert closed.exit_reason == "STOP"


def test_session_state_rejects_naive_last_bar_time() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        SessionState(
            session_date=date(2026, 7, 15),
            last_bar_time=datetime(2026, 7, 15, 15, 0),
        )


def test_session_state_normalises_aware_last_bar_time_to_utc() -> None:
    eastern_daylight = timezone(timedelta(hours=-4))
    state = SessionState(
        session_date=date(2026, 7, 15),
        last_bar_time=datetime(2026, 7, 15, 11, 0, tzinfo=eastern_daylight),
    )
    assert state.last_bar_time == datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
    assert state.last_bar_time.utcoffset() == timedelta(0)


def test_every_datetime_field_converts_a_non_utc_offset() -> None:
    """Guards the .astimezone() call itself, which a UTC-in test cannot reach."""
    eastern_daylight = timezone(timedelta(hours=-4))
    local = datetime(2026, 7, 15, 9, 30, tzinfo=eastern_daylight)
    expected = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)

    assert Tick(symbol="NQ", ts=local, price=Decimal("1"), size=1).ts == expected
    assert _bar(open_time=local).open_time == expected
    assert _entry(timestamp=local).timestamp == expected

    position = Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        entry_price=Decimal("20100"),
        entry_time=local,
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
    )
    assert position.entry_time == expected
    assert PositionClose(
        position=position, exit_price=Decimal("1"), exit_time=local, exit_reason="STOP"
    ).exit_time == expected


def test_session_state_defaults_are_empty() -> None:
    state = SessionState(session_date=date(2026, 7, 15))
    assert state.trades_taken == 0
    assert state.is_halted is False
    assert state.strategy_state == {}
    assert state.last_bar_time is None
    assert state.position is None
