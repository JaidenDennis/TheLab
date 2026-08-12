import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from nq_agent.backtest import BacktestReport, Trade, load_trades, run_backtest
from nq_agent.models import Direction

SESSION = date(2026, 7, 15)
OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
FIXTURE = Path("tests/fixtures/2026-07-15.jsonl")


def trade(
    entry: str,
    exit_: str,
    direction: Direction = Direction.LONG,
    quantity: int = 1,
    reason: str = "TARGET",
) -> Trade:
    return Trade(
        session_date=SESSION,
        direction=direction,
        quantity=quantity,
        entry_price=Decimal(entry),
        entry_time=OPEN,
        exit_price=Decimal(exit_),
        exit_time=OPEN + timedelta(minutes=5),
        exit_reason=reason,
    )


def report(*trades: Trade, commission: str = "0") -> BacktestReport:
    return BacktestReport(
        trades=list(trades),
        point_value=Decimal("20"),
        commission_per_round_turn=Decimal(commission),
    )


def test_a_long_that_rises_makes_points() -> None:
    assert trade("20100", "20120").points == Decimal("20")


def test_a_short_that_rises_loses_points() -> None:
    """Direction has to invert the arithmetic, or every short reads backwards."""
    assert trade("20100", "20120", direction=Direction.SHORT).points == Decimal("-20")


def test_a_short_that_falls_makes_points() -> None:
    assert trade("20100", "20080", direction=Direction.SHORT).points == Decimal("20")


def test_quantity_multiplies_the_result() -> None:
    assert trade("20100", "20120", quantity=3).points_total == Decimal("60")


def test_dollars_use_the_configured_point_value() -> None:
    """NQ is $20/point. Getting this wrong scales every number in the report
    without making any of them look wrong."""
    assert report(trade("20100", "20120")).net_dollars == Decimal("400")


def test_commission_is_charged_per_round_turn_per_contract() -> None:
    result = report(trade("20100", "20120", quantity=2), commission="5")

    assert result.gross_dollars == Decimal("800")
    assert result.net_dollars == Decimal("790")


def test_win_rate_counts_only_closed_trades() -> None:
    result = report(
        trade("20100", "20120"),
        trade("20100", "20090"),
        trade("20100", "20110"),
    )

    assert result.wins == 2
    assert result.losses == 1
    assert result.win_rate == pytest.approx(2 / 3)


def test_a_scratch_is_neither_a_win_nor_a_loss() -> None:
    result = report(trade("20100", "20100"))

    assert (result.wins, result.losses, result.scratches) == (0, 0, 1)


def test_profit_factor_is_gross_wins_over_gross_losses() -> None:
    result = report(trade("20100", "20130"), trade("20100", "20090"))

    assert result.profit_factor == Decimal("3")


def test_profit_factor_is_none_when_nothing_lost() -> None:
    """Dividing by zero losses would either crash the report or print inf.
    Neither is a number anyone should act on."""
    assert report(trade("20100", "20130")).profit_factor is None


def test_max_drawdown_measures_the_worst_peak_to_trough() -> None:
    """Equity runs +30, -10, -10, +5 => peak 30, trough 10, drawdown 20 points."""
    result = report(
        trade("20100", "20130"),
        trade("20100", "20090"),
        trade("20100", "20090"),
        trade("20100", "20105"),
    )

    assert result.max_drawdown_points == Decimal("20")


def test_max_drawdown_is_zero_for_a_run_that_never_gives_back() -> None:
    result = report(trade("20100", "20110"), trade("20100", "20120"))

    assert result.max_drawdown_points == Decimal("0")


def test_expectancy_is_average_points_per_trade() -> None:
    result = report(trade("20100", "20130"), trade("20100", "20090"))

    assert result.expectancy_points == Decimal("10")


def test_an_empty_backtest_reports_nothing_rather_than_dividing_by_zero() -> None:
    result = report()

    assert result.trades == []
    assert result.win_rate is None
    assert result.expectancy_points is None
    assert "no trades" in result.render().lower()


def test_the_report_states_what_it_does_not_model() -> None:
    """A P&L figure with no stated assumptions is the most misleading thing
    this repo can produce."""
    rendered = report(trade("20100", "20120")).render()

    assert "slippage" in rendered.lower()
    assert "commission" in rendered.lower()


def test_the_report_flags_unmodelled_commission(tmp_path: Path) -> None:
    rendered = report(trade("20100", "20120"), commission="0").render()

    assert "not modelled" in rendered.lower()


def write_journal(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / "2026-07-15.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return journal_dir


def test_trades_are_loaded_from_the_journal(tmp_path: Path) -> None:
    journal_dir = write_journal(
        tmp_path,
        [
            {"ts": OPEN.isoformat(), "event": "session_start", "strategy": "x"},
            {
                "ts": OPEN.isoformat(),
                "event": "position_closed",
                "direction": "LONG",
                "quantity": 2,
                "entry_price": "20100.00",
                "entry_time": OPEN.isoformat(),
                "exit_price": "20120.00",
                "exit_time": (OPEN + timedelta(minutes=5)).isoformat(),
                "exit_reason": "TARGET",
            },
        ],
    )

    trades = load_trades(journal_dir)

    assert len(trades) == 1
    assert trades[0].direction is Direction.LONG
    assert trades[0].quantity == 2
    assert trades[0].points == Decimal("20")
    assert trades[0].session_date == SESSION


def test_prices_load_as_decimals_not_floats(tmp_path: Path) -> None:
    """The journal stores Decimal as a quoted string precisely so it survives
    the round trip. Parsing it back through float would undo that."""
    journal_dir = write_journal(
        tmp_path,
        [
            {
                "ts": OPEN.isoformat(),
                "event": "position_closed",
                "direction": "LONG",
                "quantity": 1,
                "entry_price": "20100.10",
                "entry_time": OPEN.isoformat(),
                "exit_price": "20100.30",
                "exit_time": OPEN.isoformat(),
                "exit_reason": "TARGET",
            }
        ],
    )

    loaded = load_trades(journal_dir)[0]

    assert isinstance(loaded.entry_price, Decimal)
    assert loaded.points == Decimal("0.20")


def test_a_journal_with_no_closes_yields_no_trades(tmp_path: Path) -> None:
    journal_dir = write_journal(
        tmp_path, [{"ts": OPEN.isoformat(), "event": "session_start", "strategy": "x"}]
    )

    assert load_trades(journal_dir) == []


async def test_a_backtest_over_the_fixture_produces_a_report(tmp_path: Path) -> None:
    """End to end: the always strategy takes exactly one trade in this
    fixture and it reaches the target."""
    config = tmp_path / "bt.yaml"
    config.write_text("timeframes: [1m, 5m]\nexecutors: []\n")

    result = await run_backtest(config, [FIXTURE], "always", out_dir=tmp_path / "out")

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "TARGET"
    assert result.net_dollars > 0


async def test_a_backtest_does_not_write_into_the_configs_data_dir(
    tmp_path: Path,
) -> None:
    """A backtest must never touch the live journal or state database."""
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    config = tmp_path / "bt.yaml"
    config.write_text(f"data_dir: {live_dir.as_posix()}\ntimeframes: [1m]\nexecutors: []\n")

    await run_backtest(config, [FIXTURE], "always", out_dir=tmp_path / "out")

    assert list(live_dir.iterdir()) == [], "the backtest wrote into the live data_dir"
