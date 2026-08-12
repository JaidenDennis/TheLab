"""Money-aware risk. The existing limits count trades; these count dollars.

A prop account does not die from taking too many trades. It dies from a daily
loss limit or a trailing drawdown breach, and when it does, the account is
gone -- there is no drawing it back the next morning. Every check here vetoes
ENTRY only; FLATTEN still bypasses everything, because a limit that traps you
in a losing position is not a risk control.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SessionCalendar
from nq_agent.models import Direction, Signal, SignalIntent, VetoReason
from nq_agent.risk.limits import RiskManager

OPEN = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


def calendar() -> SessionCalendar:
    return SessionCalendar("America/New_York", time(9, 30), time(16, 30))


def manager(tmp_path: Path, **kwargs: object) -> RiskManager:
    defaults: dict[str, object] = {
        "calendar": calendar(),
        "max_trades_per_day": 99,
        "duplicate_window_seconds": 0,
        "kill_switch_path": tmp_path / "halt",
    }
    defaults.update(kwargs)
    return RiskManager(**defaults)  # type: ignore[arg-type]


def entry(minute: int = 0) -> Signal:
    return Signal(
        timestamp=OPEN + timedelta(minutes=minute),
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.LONG,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
        quantity=1,
        reason="test",
    )


def flatten(minute: int = 0) -> Signal:
    return Signal(
        timestamp=OPEN + timedelta(minutes=minute),
        symbol="NQ",
        intent=SignalIntent.FLATTEN,
        direction=Direction.LONG,
        quantity=1,
        reason="cutoff",
    )


def test_no_pnl_limits_configured_means_no_pnl_vetoes(tmp_path: Path) -> None:
    """Opt-in. An existing config with no money limits behaves exactly as before."""
    risk = manager(tmp_path)

    assert risk.check(entry(), trades_taken=0, enabled_executor_count=1) is None


def test_a_loss_inside_the_daily_limit_still_trades(tmp_path: Path) -> None:
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-300"))

    assert risk.check(entry(), trades_taken=0, enabled_executor_count=1) is None


def test_reaching_the_daily_loss_limit_vetoes_further_entries(tmp_path: Path) -> None:
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-500"))

    veto = risk.check(entry(), trades_taken=0, enabled_executor_count=1)

    assert veto is not None
    assert veto.reason is VetoReason.DAILY_LOSS_LIMIT


def test_the_daily_loss_limit_is_a_magnitude_not_a_signed_number(tmp_path: Path) -> None:
    """max_daily_loss: 500 means "stop after losing 500", however the operator
    writes it. Reading it as a signed floor would invert the whole control."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-600"))

    assert risk.check(entry(), trades_taken=0, enabled_executor_count=1) is not None


def test_profits_do_not_trip_the_daily_loss_limit(tmp_path: Path) -> None:
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("5000"))

    assert risk.check(entry(), trades_taken=0, enabled_executor_count=1) is None


def test_a_flatten_is_never_blocked_by_the_daily_loss_limit(tmp_path: Path) -> None:
    """The most important line in this file. Hitting the loss limit while
    holding an open position must not strand you in it."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-5000"))

    assert risk.check(flatten(), trades_taken=0, enabled_executor_count=1) is None


def test_trailing_drawdown_measures_from_the_high_water_mark(tmp_path: Path) -> None:
    """Up 1000, then down to 600, is a 400 drawdown -- even though the account
    is still up on the day. This is the rule that surprises people, and it is
    the one that closes prop accounts."""
    risk = manager(tmp_path, max_trailing_drawdown=Decimal("500"))
    risk.record_realised_pnl(Decimal("1000"))
    risk.record_realised_pnl(Decimal("-400"))

    assert risk.check(entry(), trades_taken=0, enabled_executor_count=1) is None

    risk.record_realised_pnl(Decimal("-100"))
    veto = risk.check(entry(), trades_taken=0, enabled_executor_count=1)

    assert veto is not None
    assert veto.reason is VetoReason.TRAILING_DRAWDOWN


def test_the_high_water_mark_never_moves_down(tmp_path: Path) -> None:
    risk = manager(tmp_path, max_trailing_drawdown=Decimal("500"))
    risk.record_realised_pnl(Decimal("1000"))
    risk.record_realised_pnl(Decimal("-900"))  # equity 100, drawdown 900

    assert risk.check(entry(), trades_taken=0, enabled_executor_count=1) is not None


def test_a_flatten_is_never_blocked_by_the_trailing_drawdown(tmp_path: Path) -> None:
    risk = manager(tmp_path, max_trailing_drawdown=Decimal("100"))
    risk.record_realised_pnl(Decimal("-5000"))

    assert risk.check(flatten(), trades_taken=0, enabled_executor_count=1) is None


def test_the_daily_figure_resets_on_a_new_session(tmp_path: Path) -> None:
    """Daily loss is daily. Yesterday's losing day must not veto today."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-600"))

    risk.start_session(date(2026, 7, 16))

    assert risk.check(entry(), trades_taken=0, enabled_executor_count=1) is None


def test_the_trailing_drawdown_does_NOT_reset_on_a_new_session(tmp_path: Path) -> None:
    """Trailing drawdown is measured across the life of the account, not the
    day. Resetting it nightly would defeat the control entirely -- and the
    prop firm is not resetting it."""
    risk = manager(tmp_path, max_trailing_drawdown=Decimal("500"))
    risk.record_realised_pnl(Decimal("-600"))

    risk.start_session(date(2026, 7, 16))

    veto = risk.check(entry(), trades_taken=0, enabled_executor_count=1)
    assert veto is not None
    assert veto.reason is VetoReason.TRAILING_DRAWDOWN


def test_pnl_state_survives_a_restart(tmp_path: Path) -> None:
    """A restart that forgets the day's losses is a restart that resumes
    trading straight through the limit."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-600"))

    revived = manager(tmp_path, max_daily_loss=Decimal("500"))
    revived.restore(risk.snapshot())

    assert revived.check(entry(), trades_taken=0, enabled_executor_count=1) is not None


def test_the_kill_switch_still_outranks_the_pnl_checks(tmp_path: Path) -> None:
    halt = tmp_path / "halt"
    halt.write_text("")
    risk = manager(tmp_path, max_daily_loss=Decimal("500"), kill_switch_path=halt)
    risk.record_realised_pnl(Decimal("-600"))

    veto = risk.check(entry(), trades_taken=0, enabled_executor_count=1)

    assert veto is not None
    assert veto.reason is VetoReason.KILL_SWITCH


# --- end to end: the limit has to actually stop a running session ---


class TightStopEveryBar:
    """Enters on every 1m bar with a 1-point stop and an unreachable target,
    so the first trade is guaranteed to lose and there is always another entry
    behind it for the limit to veto. ORB fires once a session and so cannot
    show this."""

    name = "tight_stop_every_bar"
    required_timeframes = ["1m"]

    def on_bar(self, bar, context):  # type: ignore[no-untyped-def]
        if bar.timeframe != "1m":
            return None
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            direction=Direction.LONG,
            entry_price=bar.close,
            stop_price=bar.close - Decimal("1"),
            target_price=bar.close + Decimal("5000"),
            quantity=1,
            reason="tight stop",
        )

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict:
        return {}

    def restore_state(self, state: dict) -> None:
        return None


async def test_a_daily_loss_limit_stops_a_running_session(tmp_path: Path) -> None:
    """Unit tests prove the veto fires. This proves the engine feeds it: a
    losing trade has to reach record_realised_pnl, or the limit sits at zero
    all day and never trips."""
    import json

    from nq_agent.main import run_from_config

    fixture = Path("tests/fixtures/2026-07-15.jsonl")
    config = tmp_path / "risk.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m, 5m]\n"
        "risk:\n"
        "  max_trades_per_day: 100\n"
        "  duplicate_window_seconds: 0\n"
        "  max_daily_loss: 1\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify]\n"
    )

    await run_from_config(
        config, fixture, "stub", None, strategy_override=TightStopEveryBar()  # type: ignore[arg-type]
    )

    records = [
        json.loads(line)
        for line in (tmp_path / "journal" / "2026-07-15.jsonl").read_text().splitlines()
    ]
    closes = [r for r in records if r["event"] == "position_closed"]
    vetoes = [r for r in records if r["event"] == "risk_veto"]

    assert closes, "precondition: the reference strategy must take a trade here"
    assert Decimal(str(closes[0]["realised_pnl"])) < 0, "precondition: that trade must lose"
    assert any(v["reason"] == VetoReason.DAILY_LOSS_LIMIT.value for v in vetoes), (
        "a losing trade past the daily limit must veto the next entry; "
        f"vetoes seen: {[v['reason'] for v in vetoes]}"
    )


async def test_realised_pnl_uses_the_configured_point_value(tmp_path: Path) -> None:
    """MNQ is a tenth of NQ. Getting point_value wrong scales every risk
    limit by 10x without any error."""
    import json

    from nq_agent.main import run_from_config

    fixture = Path("tests/fixtures/2026-07-15.jsonl")

    async def pnl_with(point_value: str) -> Decimal:
        run_dir = tmp_path / f"pv{point_value}"
        config = run_dir / "c.yaml"
        run_dir.mkdir(parents=True, exist_ok=True)
        config.write_text(
            f"data_dir: {run_dir.as_posix()}\n"
            "timeframes: [1m, 5m]\n"
            "contract:\n"
            f"  point_value: {point_value}\n"
            "executors:\n"
            "  - name: dryrun_broker\n"
            "    type: dryrun\n"
            "    enabled: true\n"
            "    accounts: [tradeify]\n"
        )
        await run_from_config(config, fixture, "orb", None)
        records = [
            json.loads(line)
            for line in (run_dir / "journal" / "2026-07-15.jsonl").read_text().splitlines()
        ]
        closes = [r for r in records if r["event"] == "position_closed"]
        return Decimal(str(closes[0]["realised_pnl"]))

    nq = await pnl_with("20")
    mnq = await pnl_with("2")

    assert nq == mnq * 10


def test_the_duplicate_window_survives_a_restart(tmp_path: Path) -> None:
    """Gap 8. A crash loop is exactly when a strategy re-fires the same
    signal, so an in-memory-only guard is blind at the moment it matters."""
    risk = manager(tmp_path, duplicate_window_seconds=60)
    risk.record_accepted(entry(minute=0))

    revived = manager(tmp_path, duplicate_window_seconds=60)
    revived.restore(risk.snapshot())

    veto = revived.check(entry(minute=0), trades_taken=0, enabled_executor_count=1)
    assert veto is not None
    assert veto.reason is VetoReason.DUPLICATE_SIGNAL


def test_a_restored_duplicate_window_still_expires(tmp_path: Path) -> None:
    """Restoring the window must not make it permanent."""
    risk = manager(tmp_path, duplicate_window_seconds=60)
    risk.record_accepted(entry(minute=0))

    revived = manager(tmp_path, duplicate_window_seconds=60)
    revived.restore(risk.snapshot())

    assert revived.check(entry(minute=5), trades_taken=0, enabled_executor_count=1) is None
