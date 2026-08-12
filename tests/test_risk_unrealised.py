"""Risk on the position you are still holding.

The existing limits act on closed trades. A prop firm's trailing drawdown
watches your equity including the open position, so it can breach you while
the agent believes it is comfortably inside its own limits -- the position is
down 800, the firm has closed the account, and the agent has recorded a
realised loss of zero.

Vetoing entries is not a sufficient response here. You are already in the
trade. The only thing that helps is getting out, so a breach on an open
position emits a protective FLATTEN.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SessionCalendar
from nq_agent.models import Direction, Position, VetoReason
from nq_agent.risk.limits import RiskManager

OPEN = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


def manager(tmp_path: Path, **kwargs: object) -> RiskManager:
    defaults: dict[str, object] = {
        "calendar": SessionCalendar("America/New_York", time(9, 30), time(16, 30)),
        "max_trades_per_day": 99,
        "duplicate_window_seconds": 0,
        "kill_switch_path": tmp_path / "halt",
    }
    defaults.update(kwargs)
    return RiskManager(**defaults)  # type: ignore[arg-type]


def long_at(entry: str, quantity: int = 1) -> Position:
    return Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=quantity,
        entry_price=Decimal(entry),
        entry_time=OPEN,
        stop_price=Decimal("19000"),
        target_price=Decimal("21000"),
    )


def short_at(entry: str, quantity: int = 1) -> Position:
    return Position(
        symbol="NQ",
        direction=Direction.SHORT,
        quantity=quantity,
        entry_price=Decimal(entry),
        entry_time=OPEN,
        stop_price=Decimal("21000"),
        target_price=Decimal("19000"),
    )


POINT = Decimal("20")  # NQ


def _reason(risk: RiskManager) -> VetoReason | None:
    breach = risk.breach()
    return breach.reason if breach is not None else None


# --- marking to market ------------------------------------------------------


def test_a_long_moving_against_you_marks_negative(tmp_path: Path) -> None:
    risk = manager(tmp_path)

    risk.mark_to_market(long_at("20100"), Decimal("20090"), POINT)

    assert risk.unrealised == Decimal("-200")


def test_a_short_moving_against_you_marks_negative(tmp_path: Path) -> None:
    """Direction has to invert or every short reads backwards -- and reads as
    a profit at the exact moment it is losing."""
    risk = manager(tmp_path)

    risk.mark_to_market(short_at("20100"), Decimal("20110"), POINT)

    assert risk.unrealised == Decimal("-200")


def test_quantity_multiplies_the_mark(tmp_path: Path) -> None:
    risk = manager(tmp_path)

    risk.mark_to_market(long_at("20100", quantity=3), Decimal("20090"), POINT)

    assert risk.unrealised == Decimal("-600")


def test_marking_flat_clears_the_open_figure(tmp_path: Path) -> None:
    """Once the position closes its result is realised. Leaving the mark
    behind would count the same money twice."""
    risk = manager(tmp_path)
    risk.mark_to_market(long_at("20100"), Decimal("20090"), POINT)

    risk.mark_to_market(None, Decimal("20090"), POINT)

    assert risk.unrealised == Decimal("0")


# --- the daily loss limit sees the open position ---------------------------


def test_an_open_loss_counts_toward_the_daily_limit(tmp_path: Path) -> None:
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))

    risk.mark_to_market(long_at("20100"), Decimal("20070"), POINT)  # -600

    veto = risk.check(_entry(), trades_taken=0, enabled_executor_count=1)
    assert veto is not None
    assert veto.reason is VetoReason.DAILY_LOSS_LIMIT


def test_an_open_loss_combines_with_realised_losses(tmp_path: Path) -> None:
    """Neither alone breaches; together they do. Checking them separately is
    how an account dies inside two limits that both looked fine."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-300"))

    risk.mark_to_market(long_at("20100"), Decimal("20090"), POINT)  # -200

    assert risk.check(_entry(), trades_taken=0, enabled_executor_count=1) is not None


def test_an_open_profit_does_not_mask_a_realised_loss_beyond_the_limit(
    tmp_path: Path,
) -> None:
    """An unrealised gain is not money you have. It is allowed to keep you
    trading, but only because the firm counts it the same way -- this pins
    the behaviour so it is a decision rather than an accident."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-600"))

    risk.mark_to_market(long_at("20100"), Decimal("20200"), POINT)  # +2000

    assert risk.check(_entry(), trades_taken=0, enabled_executor_count=1) is None


# --- trailing drawdown on equity -------------------------------------------


def test_trailing_drawdown_counts_the_open_position(tmp_path: Path) -> None:
    risk = manager(tmp_path, max_trailing_drawdown=Decimal("500"))
    risk.record_realised_pnl(Decimal("1000"))

    risk.mark_to_market(long_at("20100"), Decimal("20070"), POINT)  # -600, equity 400

    veto = risk.check(_entry(), trades_taken=0, enabled_executor_count=1)
    assert veto is not None
    assert veto.reason is VetoReason.TRAILING_DRAWDOWN


def test_an_open_profit_raises_the_high_water_mark(tmp_path: Path) -> None:
    """The trap. Being up 1000 on an open position sets the mark, and giving
    part of it back is a drawdown even though nothing was ever realised and
    the account is still up on the day. Firms that trail on intraday equity
    work exactly this way.

    The second mark stays POSITIVE on purpose. Ending at a loss would breach
    the limit on the open loss alone, and the test would pass whether or not
    the peak had ever moved the mark -- which is the only thing it is here to
    pin. At +400 against a 1000 peak, nothing but a raised high-water mark
    can produce a 600 drawdown.
    """
    risk = manager(tmp_path, max_trailing_drawdown=Decimal("500"))

    risk.mark_to_market(long_at("20100"), Decimal("20150"), POINT)  # +1000, sets the mark
    risk.mark_to_market(long_at("20100"), Decimal("20120"), POINT)  # +400, still in profit

    veto = risk.check(_entry(), trades_taken=0, enabled_executor_count=1)
    assert veto is not None, "gave back 600 of a 1000 open profit and saw no drawdown"
    assert veto.reason is VetoReason.TRAILING_DRAWDOWN


def test_the_closed_basis_ignores_open_profit_for_the_high_water_mark(
    tmp_path: Path,
) -> None:
    """Firms differ. On a closed-balance basis an unrealised peak does not
    move the mark, so giving it back is not a drawdown."""
    risk = manager(
        tmp_path,
        max_trailing_drawdown=Decimal("500"),
        trailing_drawdown_basis="closed",
    )

    risk.mark_to_market(long_at("20100"), Decimal("20150"), POINT)  # +1000
    risk.mark_to_market(long_at("20100"), Decimal("20100"), POINT)  # back to flat

    assert risk.check(_entry(), trades_taken=0, enabled_executor_count=1) is None


def test_the_closed_basis_still_counts_open_losses_against_the_mark(
    tmp_path: Path,
) -> None:
    """Not moving the peak is not the same as ignoring the position. A loss
    on an open trade still eats into the distance from the mark."""
    risk = manager(
        tmp_path,
        max_trailing_drawdown=Decimal("500"),
        trailing_drawdown_basis="closed",
    )
    risk.record_realised_pnl(Decimal("1000"))

    risk.mark_to_market(long_at("20100"), Decimal("20070"), POINT)  # -600

    assert risk.check(_entry(), trades_taken=0, enabled_executor_count=1) is not None


# --- the breach has to produce an exit, not just a veto --------------------


def test_no_breach_reported_when_inside_the_limits(tmp_path: Path) -> None:
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.mark_to_market(long_at("20100"), Decimal("20095"), POINT)

    assert risk.breach() is None


def test_a_breach_is_reported_so_the_engine_can_flatten(tmp_path: Path) -> None:
    """Vetoing entries does nothing when you are already in the trade. The
    only thing that helps is getting out."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))

    risk.mark_to_market(long_at("20100"), Decimal("20070"), POINT)  # -600

    assert _reason(risk) is VetoReason.DAILY_LOSS_LIMIT


def test_a_drawdown_breach_is_reported(tmp_path: Path) -> None:
    risk = manager(tmp_path, max_trailing_drawdown=Decimal("500"))
    risk.record_realised_pnl(Decimal("1000"))

    risk.mark_to_market(long_at("20100"), Decimal("20070"), POINT)

    assert _reason(risk) is VetoReason.TRAILING_DRAWDOWN


def test_a_realised_only_breach_is_still_reported(tmp_path: Path) -> None:
    """So a limit reached by closed trades alone also stops the day, rather
    than only blocking the next entry."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-600"))

    assert _reason(risk) is VetoReason.DAILY_LOSS_LIMIT


def test_the_open_mark_survives_a_restart(tmp_path: Path) -> None:
    """The high-water mark it produced must not be forgotten, or a restart
    resets the drawdown measurement to whatever equity happens to be."""
    risk = manager(tmp_path, max_trailing_drawdown=Decimal("500"))
    risk.mark_to_market(long_at("20100"), Decimal("20150"), POINT)  # peak +1000

    revived = manager(tmp_path, max_trailing_drawdown=Decimal("500"))
    revived.restore(risk.snapshot())
    revived.mark_to_market(long_at("20100"), Decimal("20075"), POINT)  # -500

    assert _reason(revived) is VetoReason.TRAILING_DRAWDOWN


def _entry():
    from nq_agent.models import Signal, SignalIntent

    return Signal(
        timestamp=OPEN + timedelta(minutes=1),
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.LONG,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
        quantity=1,
        reason="test",
    )


def test_session_rollover_keeps_the_high_water_mark(tmp_path: Path) -> None:
    """Daily figures reset; the account-lifetime mark does not."""
    risk = manager(tmp_path, max_trailing_drawdown=Decimal("500"))
    risk.record_realised_pnl(Decimal("1000"))

    risk.start_session(date(2026, 7, 16))
    risk.record_realised_pnl(Decimal("-600"))

    assert _reason(risk) is VetoReason.TRAILING_DRAWDOWN


# --- a carried position and the daily limit --------------------------------


def test_a_carried_position_charges_only_todays_move_to_the_daily_limit(
    tmp_path: Path,
) -> None:
    """Yesterday's open loss is yesterday's. The firm's daily limit measures
    today, so a position that survived the boundary re-bases at the rollover
    and only its move from there counts against the fresh limit."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.mark_to_market(long_at("20100"), Decimal("20070"), POINT)  # -600 since entry

    risk.start_session(date(2026, 7, 16))
    risk.mark_to_market(long_at("20100"), Decimal("20070"), POINT)  # unchanged today

    assert risk.breach() is None, "yesterday's move was charged to today's limit"

    risk.mark_to_market(long_at("20100"), Decimal("20040"), POINT)  # -600 more, today

    assert _reason(risk) is VetoReason.DAILY_LOSS_LIMIT


def test_the_rollover_mark_clears_when_the_position_closes(tmp_path: Path) -> None:
    """The re-base belonged to the carried position. A new trade opened later
    the same day measures from zero -- a stale offset would let it hide a
    fresh loss exactly the size of the old one."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.mark_to_market(long_at("20100"), Decimal("20070"), POINT)  # -600 carried
    risk.start_session(date(2026, 7, 16))

    risk.mark_to_market(None, Decimal("20070"), POINT)  # carried position closes
    risk.mark_to_market(long_at("20070"), Decimal("20040"), POINT)  # new trade, -600

    assert _reason(risk) is VetoReason.DAILY_LOSS_LIMIT


def test_the_rollover_mark_survives_a_restart(tmp_path: Path) -> None:
    """A restart mid-session with a carried position must not re-base the
    mark to zero, or the whole since-entry move is charged to today again."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.mark_to_market(long_at("20100"), Decimal("20070"), POINT)  # -600 carried
    risk.start_session(date(2026, 7, 16))

    revived = manager(tmp_path, max_daily_loss=Decimal("500"))
    revived.restore(risk.snapshot())
    revived.mark_to_market(long_at("20100"), Decimal("20070"), POINT)  # unchanged today

    assert revived.breach() is None


def test_the_breach_detail_reports_the_figures_that_tripped_it(tmp_path: Path) -> None:
    """The journaled detail is computed alongside the decision, so the number
    an operator reads is the number that was measured."""
    risk = manager(tmp_path, max_daily_loss=Decimal("500"))
    risk.record_realised_pnl(Decimal("-300"))
    risk.mark_to_market(long_at("20100"), Decimal("20090"), POINT)  # -200

    breach = risk.breach()
    assert breach is not None
    assert "down 500" in breach.detail
    assert "realised -300" in breach.detail


# --- end to end: the breach has to reach a broker --------------------------


async def test_an_open_loss_breach_flattens_the_position(tmp_path: Path) -> None:
    """The whole point. A veto cannot help once the position is on, so the
    engine has to turn a breach into an exit that reaches an executor."""
    import json

    from nq_agent.main import run_from_config

    config = tmp_path / "u.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m, 5m]\n"
        "contract:\n"
        "  point_value: 20\n"
        "risk:\n"
        "  max_trades_per_day: 10\n"
        "  duplicate_window_seconds: 0\n"
        "  max_daily_loss: 20\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify]\n"
    )

    await run_from_config(config, Path("tests/fixtures/2026-07-15.jsonl"), "always", None)

    records = [
        json.loads(line)
        for line in (tmp_path / "journal" / "2026-07-15.jsonl").read_text().splitlines()
    ]
    flattens = [r for r in records if r["event"] == "risk_flatten"]
    assert flattens, "an open position past the daily limit was never flattened"
    assert flattens[0]["reason"] == VetoReason.DAILY_LOSS_LIMIT.value

    closed = [r for r in records if r["event"] == "position_closed"]
    assert closed, "the flatten never closed anything"
    assert closed[0]["exit_reason"] == "FLATTEN"

    orders = [r for r in records if r["event"] == "order_result"]
    flatten_orders = [o for o in orders if o["signal_id"] == flattens[0]["signal_id"]]
    assert flatten_orders, "the protective flatten never reached an executor"


async def test_no_risk_flatten_without_limits_configured(tmp_path: Path) -> None:
    """Opt-in, like every other money limit. An unconfigured run must behave
    exactly as it did before."""
    import json

    from nq_agent.main import run_from_config

    config = tmp_path / "n.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m, 5m]\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify]\n"
    )

    await run_from_config(config, Path("tests/fixtures/2026-07-15.jsonl"), "always", None)

    records = [
        json.loads(line)
        for line in (tmp_path / "journal" / "2026-07-15.jsonl").read_text().splitlines()
    ]
    assert not [r for r in records if r["event"] == "risk_flatten"]
