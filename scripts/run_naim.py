"""NAIM ablation & sweep runner, spec v1.0 sections 9-10.

All variants are declared HERE, before any results exist, per the
pre-registration discipline. Every variant runs the same engine over the
same fixtures and the same sample window (2020-07-01 onward, so every
lookback's curves exist from the first session and the samples are
identical), into its own isolated directory.

The sanity anchor (spec 9.4) is the `core` variant: L=90, 30-minute
boundaries, no OFI gate, close-based band+VWAP structural stop, FOMC
calendar populated. Run it FIRST and compare its shape to the published NQ
replication (win ~35-40%, payoff ~2+, positive EV) before believing any
other cell.

Usage:
  uv run python scripts/run_naim.py --fixtures var/fixtures/1m \
      --noise var/noise --start 2020-07-01 --end 2024-09-30 \
      --out var/naim --variant core
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, time
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

from nq_agent.backtest import BacktestReport, run_backtest
from nq_agent.strategy.naim import NoiseAreaIntradayMomentum

FOMC_PATH = Path("config/fomc_dates.json")


def fomc_dates() -> set[date]:
    payload = json.loads(FOMC_PATH.read_text(encoding="utf-8"))
    return {date.fromisoformat(d) for d in payload["dates"]}


def load_curves(noise_dir: Path, lookback: int) -> dict[str, Any]:
    return json.loads((noise_dir / f"L{lookback}.json").read_text(encoding="utf-8"))


def build_variants(noise_dir: Path) -> dict[str, partial[NoiseAreaIntradayMomentum]]:
    curves = {lb: load_curves(noise_dir, lb) for lb in (14, 30, 60, 90, 120)}
    fomc = fomc_dates()

    def base(**overrides: Any) -> partial[NoiseAreaIntradayMomentum]:
        settings: dict[str, Any] = dict(
            noise_curves=curves[90],
            trigger_mode="30m",
            ofi_mode="off",
            stop_mode="close",
            vwap_stop=True,
            max_entries_per_day=4,
            fomc_dates=fomc,
        )
        settings.update(overrides)
        return partial(NoiseAreaIntradayMomentum, **settings)

    return {
        # 1. sanity anchor -- the published replication's configuration
        "core": base(),
        # 2. trigger cadence
        "core_1m": base(trigger_mode="1m"),
        # 3. lookback sweep
        "core_L14": base(noise_curves=curves[14]),
        "core_L30": base(noise_curves=curves[30]),
        "core_L60": base(noise_curves=curves[60]),
        "core_L120": base(noise_curves=curves[120]),
        # 4. stop construction
        "core_touch": base(stop_mode="touch"),
        "core_novwap": base(vwap_stop=False),
        # 5. the headline ablation: +OFI gate (proxy)
        "core_gate": base(ofi_mode="proxy"),
        "core_1m_gate": base(trigger_mode="1m", ofi_mode="proxy"),
        # 6. calendar off (quantifies what SME never measured)
        "core_nocal": base(fomc_dates=set()),
        # 7. entry window / attempt cap
        "core_end13": base(entry_end=time(13, 0)),
        "core_end14": base(entry_end=time(14, 0)),
        "core_cap2": base(max_entries_per_day=2),
        "core_cap6": base(max_entries_per_day=6),
    }


def base_config(out_root: Path) -> Path:
    config = out_root / "naim-base.yaml"
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
        "avg_win_points": (
            None
            if report.average_win_points is None
            else str(report.average_win_points.quantize(Decimal("0.01")))
        ),
        "avg_loss_points": (
            None
            if report.average_loss_points is None
            else str(report.average_loss_points.quantize(Decimal("0.01")))
        ),
        "max_drawdown_points": str(report.max_drawdown_points.quantize(Decimal("0.01"))),
        "by_exit": report.by_exit_reason,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--noise", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", default="core", help="variant name or 'all'")
    args = parser.parse_args()

    variants = build_variants(args.noise)
    names = list(variants) if args.variant == "all" else [args.variant]

    fixtures = [
        path
        for path in sorted(args.fixtures.glob("*.jsonl"))
        if args.start <= date.fromisoformat(path.stem) <= args.end
    ]
    print(f"{len(fixtures)} sessions {fixtures[0].stem}..{fixtures[-1].stem}")

    args.out.mkdir(parents=True, exist_ok=True)
    config = base_config(args.out)

    summaries = []
    for name in names:
        print(f"\n=== {name} ===", flush=True)
        report = await run_backtest(
            config,
            fixtures,
            "naim",
            args.out / name,
            strategy_factory=variants[name],  # type: ignore[arg-type]
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
