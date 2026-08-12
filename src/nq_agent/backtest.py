"""Turn a run's journal into a P&L report.

The backtester deliberately owns no simulation of its own. It runs the real
engine over a fixture, through the real strategy, risk layer, router and
position tracker, and then reads the journal the run wrote -- the same
artifact a live session produces. Anything it could compute a different way
than production does is a way for a backtest to be right about a system that
does not exist.

Read the caveats in `BacktestReport.render` before believing any number here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from nq_agent.config import load_settings
from nq_agent.models import Direction
from nq_agent.strategy.base import Strategy


@dataclass(frozen=True)
class Trade:
    """One round trip, reconstructed from a position_closed journal record."""

    session_date: date
    direction: Direction
    quantity: int
    entry_price: Decimal
    entry_time: datetime
    exit_price: Decimal
    exit_time: datetime
    exit_reason: str

    @property
    def points(self) -> Decimal:
        """Signed points per contract, in the direction of the position."""
        move = self.exit_price - self.entry_price
        return move if self.direction is Direction.LONG else -move

    @property
    def points_total(self) -> Decimal:
        return self.points * self.quantity

    @property
    def duration(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds()


@dataclass(frozen=True)
class BacktestReport:
    trades: list[Trade] = field(default_factory=list)
    point_value: Decimal = Decimal("20")
    commission_per_round_turn: Decimal = Decimal("0")

    # --- money -------------------------------------------------------------

    @property
    def gross_points(self) -> Decimal:
        return sum((t.points_total for t in self.trades), Decimal("0"))

    @property
    def gross_dollars(self) -> Decimal:
        return self.gross_points * self.point_value

    @property
    def commission(self) -> Decimal:
        contracts = sum(t.quantity for t in self.trades)
        return self.commission_per_round_turn * contracts

    @property
    def net_dollars(self) -> Decimal:
        return self.gross_dollars - self.commission

    # --- counts ------------------------------------------------------------

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.points > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.points < 0)

    @property
    def scratches(self) -> int:
        return sum(1 for t in self.trades if t.points == 0)

    @property
    def win_rate(self) -> float | None:
        """None, not zero, when there are no trades: a strategy that never
        traded did not lose every trade."""
        if not self.trades:
            return None
        return self.wins / len(self.trades)

    # --- distribution ------------------------------------------------------

    @property
    def expectancy_points(self) -> Decimal | None:
        if not self.trades:
            return None
        return self.gross_points / len(self.trades)

    @property
    def average_win_points(self) -> Decimal | None:
        wins = [t.points_total for t in self.trades if t.points > 0]
        return sum(wins, Decimal("0")) / len(wins) if wins else None

    @property
    def average_loss_points(self) -> Decimal | None:
        losses = [t.points_total for t in self.trades if t.points < 0]
        return sum(losses, Decimal("0")) / len(losses) if losses else None

    @property
    def profit_factor(self) -> Decimal | None:
        """Gross wins over gross losses. None when nothing lost -- an infinite
        profit factor is not a number to act on, and a report that prints one
        invites exactly that."""
        won = sum((t.points_total for t in self.trades if t.points > 0), Decimal("0"))
        lost = sum((-t.points_total for t in self.trades if t.points < 0), Decimal("0"))
        if lost == 0:
            return None
        return won / lost

    @property
    def max_drawdown_points(self) -> Decimal:
        """Worst peak-to-trough decline of the closed-trade equity curve.

        Closed-trade, not intra-trade: the real drawdown is worse, because
        this cannot see how far a position went against itself before coming
        back. On a prop account it is the intra-trade figure that breaches a
        trailing limit, so treat this as a lower bound.
        """
        equity = Decimal("0")
        peak = Decimal("0")
        worst = Decimal("0")
        for trade in self.trades:
            equity += trade.points_total
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        return worst

    @property
    def by_exit_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for trade in self.trades:
            counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
        return counts

    @property
    def sessions(self) -> int:
        return len({t.session_date for t in self.trades})

    def render(self) -> str:
        if not self.trades:
            return (
                "No trades.\n"
                "The strategy never opened a position over this data. That is a "
                "result, not an error -- check required_timeframes and the session "
                "window before assuming the logic is wrong."
            )

        def points(value: Decimal | None) -> str:
            return "-" if value is None else f"{value:+.2f}"

        # None means "omit this line"; "" is a deliberate blank separator.
        lines: list[str | None] = [
            f"Trades            {len(self.trades)} over {self.sessions} session(s)",
            f"  won / lost      {self.wins} / {self.losses}"
            + (f" ({self.scratches} scratch)" if self.scratches else ""),
            f"  win rate        {self.win_rate:.1%}" if self.win_rate is not None else None,
            f"  by exit         {self.by_exit_reason}",
            "",
            f"Gross points      {points(self.gross_points)}",
            f"  expectancy      {points(self.expectancy_points)} per trade",
            f"  average win     {points(self.average_win_points)}",
            f"  average loss    {points(self.average_loss_points)}",
            "  profit factor   "
            + ("-" if self.profit_factor is None else f"{self.profit_factor:.2f}"),
            f"  max drawdown    -{self.max_drawdown_points:.2f} (closed-trade)",
            "",
            f"Gross P&L         ${self.gross_dollars:,.2f}  "
            f"(at ${self.point_value}/point)",
            f"Commission        ${self.commission:,.2f}"
            + (
                "   <- not modelled; set contract.commission_per_round_turn"
                if self.commission_per_round_turn == 0
                else ""
            ),
            f"Net P&L           ${self.net_dollars:,.2f}",
            "",
            "Not modelled, all of which make the real result worse:",
            "  - slippage on entry; fills are assumed at the signal's exact price",
            "  - the spread crossed on both sides",
            "  - partial fills, rejects, and any latency between signal and order",
            "  - intra-trade drawdown, which is what breaches a prop account's",
            "    trailing limit; the figure above is closed-trade only",
            "Modelled against the position: stop wins an ambiguous bar, and a bar",
            "gapping through the stop fills at the open rather than the stop.",
        ]
        return "\n".join(line for line in lines if line is not None)


def load_trades(journal_dir: Path) -> list[Trade]:
    """Every closed position in every journal file, oldest first."""
    if not journal_dir.exists():
        return []
    trades: list[Trade] = []
    for path in sorted(journal_dir.glob("*.jsonl")):
        session_date = date.fromisoformat(path.stem)
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("event") != "position_closed":
                continue
            trades.append(
                Trade(
                    session_date=session_date,
                    direction=Direction(record["direction"]),
                    quantity=int(record["quantity"]),
                    # str() first: the journal writes Decimal as a quoted
                    # string precisely so it survives the round trip, and
                    # Decimal(float) would undo that in the one place where
                    # the numbers are the whole point.
                    entry_price=Decimal(str(record["entry_price"])),
                    entry_time=datetime.fromisoformat(record["entry_time"]),
                    exit_price=Decimal(str(record["exit_price"])),
                    exit_time=datetime.fromisoformat(record["exit_time"]),
                    exit_reason=str(record["exit_reason"]),
                )
            )
    trades.sort(key=lambda t: t.exit_time)
    return trades


def _isolated_config(config_path: Path, out_dir: Path) -> Path:
    """Copy the config with data_dir pointed at the backtest's own directory.

    A backtest must not be able to write into the live journal or state
    database -- both because it would corrupt the record of what actually
    traded, and because a stale backtest SessionState would then be picked up
    as a resume by the next live start.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data["data_dir"] = str(out_dir)
    # Exactly one dry-run destination, replacing whatever the config listed.
    # Not zero: the risk layer vetoes ACCOUNT_DISABLED when no executor is
    # enabled, so an empty list silently produces a backtest in which nothing
    # ever trades. Not the config's own list either -- a notify entry would
    # page a human once per historical signal, and a webhook entry would send
    # months of orders to a broker.
    data["executors"] = [
        {"name": "backtest", "type": "dryrun", "enabled": True, "accounts": []}
    ]
    isolated = out_dir / "backtest-config.yaml"
    isolated.write_text(yaml.safe_dump(data), encoding="utf-8")
    return isolated


async def run_backtest(
    config_path: Path,
    fixtures: list[Path],
    strategy_name: str,
    out_dir: Path,
    strategy_factory: type[Strategy] | None = None,
) -> BacktestReport:
    """Replay each fixture through the real engine, then report on the journal.

    Each fixture runs as its own session. State is not shared between them
    beyond what the engine itself persists, and because every fixture carries
    its own session date, no run resumes another.
    """
    from nq_agent.main import run_from_config  # circular at module scope

    isolated = _isolated_config(config_path, out_dir)
    settings = load_settings(isolated)

    for fixture in fixtures:
        await run_from_config(
            isolated,
            fixture,
            strategy_name,
            None,
            strategy_override=strategy_factory() if strategy_factory else None,
        )

    return BacktestReport(
        trades=load_trades(settings.journal_dir),
        point_value=settings.contract.point_value,
        commission_per_round_turn=settings.contract.commission_per_round_turn,
    )


def main() -> None:
    import argparse
    import asyncio
    import logging

    parser = argparse.ArgumentParser(
        prog="nq_agent.backtest",
        description="Replay fixtures through the real engine and report P&L.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--fixture",
        type=Path,
        nargs="+",
        required=True,
        help="one or more tick fixtures; each replays as its own session",
    )
    parser.add_argument("--strategy", default="orb")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("./var/backtest"),
        help="where the backtest writes its journal and state, never the live data_dir",
    )
    args = parser.parse_args()

    # WARNING, not INFO: a multi-day backtest at INFO prints one dry-run line
    # per signal per executor and buries the report under its own noise.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    report = asyncio.run(run_backtest(args.config, args.fixture, args.strategy, args.out))
    print(report.render())


if __name__ == "__main__":
    main()
