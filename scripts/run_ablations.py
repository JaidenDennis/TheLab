"""SME ablation matrix per spec section 8.5: B alone -> A+B -> A+B+C(proxy).

Each layer must add net EV or it's cut -- so each variant runs the SAME
engine over the SAME fixtures with only the layer flags changed, into its
own isolated output directory. Costs follow section 8.3: $10/contract round
turn via contract.commission_per_round_turn (the +1-tick adverse stop fill
is inside the strategy's own fill model).

The risk governor's money limits are deliberately OFF here: the point of
the ablation is the raw EV of each layer stack, and the prop-firm
trailing-limit replay is its own protocol step (8.7) run against the
resulting equity curves, not baked into them.

Usage:
  uv run python scripts/run_ablations.py \
      --fixtures var/fixtures/1m --start 2021-01-04 --end 2024-09-30 \
      --out var/ablations
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path

from nq_agent.backtest import BacktestReport, run_backtest
from nq_agent.strategy.sme import SessionMomentumExpansion

VARIANTS: dict[str, partial[SessionMomentumExpansion]] = {
    "b_alone": partial(SessionMomentumExpansion, layer_a=False, ofi_mode="off"),
    "a_b": partial(SessionMomentumExpansion, ofi_mode="off"),
    "a_b_c_proxy": partial(SessionMomentumExpansion, ofi_mode="proxy"),
}


def base_config(out_root: Path) -> Path:
    """The section-8.3 cost model on top of the repo's base.yaml defaults.

    data_dir here is a placeholder: run_backtest rewrites it to each
    variant's own isolated directory before anything runs.
    """
    config = out_root / "ablation-base.yaml"
    config.write_text(
        f"data_dir: {out_root.as_posix()}\n"
        "timeframes: [1m, 5m]\n"
        "contract:\n"
        "  point_value: 20\n"
        "  commission_per_round_turn: 10\n"
        "risk:\n"
        "  max_trades_per_day: 50\n"
        "  duplicate_window_seconds: 0\n"
        "executors:\n"
        "  - name: backtest\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: []\n",
        encoding="utf-8",
    )
    return config


def fixtures_between(fixtures_dir: Path, start: date, end: date) -> list[Path]:
    chosen = []
    for path in sorted(fixtures_dir.glob("*.jsonl")):
        session = date.fromisoformat(path.stem)
        if start <= session <= end:
            chosen.append(path)
    return chosen


def summarise(name: str, report: BacktestReport) -> dict[str, object]:
    trades = len(report.trades)
    net = report.net_dollars.quantize(Decimal("0.01"))
    return {
        "variant": name,
        "trades": trades,
        "sessions_traded": report.sessions,
        "net_dollars": str(net),
        "ev_per_trade_dollars": str((net / trades).quantize(Decimal("0.01"))) if trades else None,
        "win_rate": None if report.win_rate is None else round(report.win_rate, 4),
        "profit_factor": (
            None
            if report.profit_factor is None
            else str(report.profit_factor.quantize(Decimal("0.01")))
        ),
        "max_drawdown_points": str(report.max_drawdown_points.quantize(Decimal("0.01"))),
        "by_exit": report.by_exit_reason,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--variant", choices=[*VARIANTS, "all"], default="all", help="run one stack, or all three"
    )
    args = parser.parse_args()

    fixtures = fixtures_between(args.fixtures, args.start, args.end)
    if not fixtures:
        raise SystemExit(f"no fixtures in {args.fixtures} for {args.start}..{args.end}")
    print(f"{len(fixtures)} sessions {fixtures[0].stem}..{fixtures[-1].stem}")

    args.out.mkdir(parents=True, exist_ok=True)
    config = base_config(args.out)
    names = list(VARIANTS) if args.variant == "all" else [args.variant]

    summaries = []
    for name in names:
        print(f"\n=== {name} ===", flush=True)
        report = await run_backtest(
            config,
            fixtures,
            "sme",
            args.out / name,
            strategy_factory=VARIANTS[name],  # type: ignore[arg-type]
        )
        print(report.render())
        summary = summarise(name, report)
        summaries.append(summary)
        (args.out / f"{name}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n=== comparison ===")
    print(
        f"{'variant':<14}{'trades':>7}{'net $':>12}{'EV/trade':>10}"
        f"{'win%':>7}{'PF':>6}{'maxDD pts':>11}"
    )
    for s in summaries:
        win = "-" if s["win_rate"] is None else f"{float(s['win_rate']):.1%}"  # type: ignore[arg-type]
        print(
            f"{s['variant']:<14}{s['trades']:>7}{s['net_dollars']:>12}"
            f"{s['ev_per_trade_dollars'] or '-':>10}{win:>7}"
            f"{s['profit_factor'] or '-':>6}{s['max_drawdown_points']:>11}"
        )


if __name__ == "__main__":
    asyncio.run(main())
