"""TFR ablation runner, spec v1.0 section 10 -- all cells pre-declared here.

The headline experiment is the exit matrix: V-HF / V-RI / V-STACK over
identical entries against the V-T13 baseline. Feature knockouts, the
regime-off control, K/refit variants and threshold sweeps follow. Every
variant runs the production engine over the same 1m fixtures with the same
decision files, into its own isolated directory.

Research window note (recorded 2026-08-13): the program runs on the plan's
free trailing tick year (2025-08 -> 2026-08) at the user's direction -- no
purchased history. Quarter-stability replaces year-stability in the gate,
and the shadow-forward gate remains the real verdict per spec section 9.

Usage:
  uv run python scripts/run_tfr.py --fixtures var/fixtures/1m \
      --decisions var/decisions/k3m --out var/tfr --variant exit_stack
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

from nq_agent.backtest import BacktestReport, run_backtest
from nq_agent.strategy.tfr import TickFlowRegime

FOMC_PATH = Path("config/fomc_dates.json")


def fomc_dates() -> set[date]:
    payload = json.loads(FOMC_PATH.read_text(encoding="utf-8"))
    return {date.fromisoformat(d) for d in payload["dates"]}


def load_decisions(decisions_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in sorted(decisions_dir.glob("*.json")):
        out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return out


def build_variants(decisions_dir: Path) -> dict[str, partial[TickFlowRegime]]:
    decisions = load_decisions(decisions_dir)
    fomc = fomc_dates()

    def base(**overrides: Any) -> partial[TickFlowRegime]:
        settings: dict[str, Any] = dict(decisions=decisions, fomc_dates=fomc)
        settings.update(overrides)
        return partial(TickFlowRegime, **settings)

    return {
        # 1. the exit matrix (headline)
        "exit_hf": base(exit_mode="hf"),
        "exit_ri": base(exit_mode="ri"),
        "exit_stack": base(exit_mode="stack"),
        "exit_t13": base(exit_mode="t13"),
        # 3. regime layer off: flow-threshold entries only, vol gate retained.
        #    (First build of this cell only lowered p_arm and still required
        #    the AF label -- it measured nothing. regime_required=False is
        #    the actual control.)
        "no_regime": base(exit_mode="stack", regime_required=False),
        # 4/5. threshold sweeps on the stack
        "stack_parm45": base(exit_mode="stack", p_arm=0.45),
        "stack_parm75": base(exit_mode="stack", p_arm=0.75),
        "stack_q60": base(exit_mode="stack", q_entry_pct=60),
        "stack_q80": base(exit_mode="stack", q_entry_pct=80),
        "stack_qhf85": base(exit_mode="stack", q_hf_pct=85),  # asymmetric, harder to shake out
        # 6. confirmation/veto features
        "stack_f4": base(exit_mode="stack", f4_confirm=True),
        "stack_f3": base(exit_mode="stack", f3_veto=True),
        # ---- v1.1 Flow Core (cycle-1 amendment, all pre-declared before
        # any v1.1 result existed; regime demoted to annotations) ----
        "fc_hf": base(exit_mode="hf", regime_required=False),
        "fc_fd": base(exit_mode="fd", regime_required=False),
        "fc_fstack": base(exit_mode="fstack", regime_required=False),
        "fc_t13": base(exit_mode="t13", regime_required=False),
        "fc_fd_h1": base(exit_mode="fd", regime_required=False, fd_hysteresis_bars=1),
        "fc_fstack_q60": base(exit_mode="fstack", regime_required=False, q_entry_pct=60),
        "fc_fstack_q80": base(exit_mode="fstack", regime_required=False, q_entry_pct=80),
        "fc_fstack_v0": base(exit_mode="fstack", regime_required=False, vol_z_min=0.0),
        "fc_fstack_v10": base(exit_mode="fstack", regime_required=False, vol_z_min=1.0),
        "fc_fstack_qhf55": base(exit_mode="fstack", regime_required=False, q_hf_pct=55),
        "fc_fstack_qhf85": base(exit_mode="fstack", regime_required=False, q_hf_pct=85),
        "fc_fstack_f4": base(exit_mode="fstack", regime_required=False, f4_confirm=True),
        "fc_fstack_f3": base(exit_mode="fstack", regime_required=False, f3_veto=True),
        "fc_fstack_cap5": base(exit_mode="fstack", regime_required=False, max_entries_per_day=5),
    }


def base_config(out_root: Path) -> Path:
    config = out_root / "tfr-base.yaml"
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
        "max_drawdown_points": str(report.max_drawdown_points.quantize(Decimal("0.01"))),
        "by_exit": report.by_exit_reason,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 8, 13))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 12))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", default="exit_stack", help="variant name or 'all'")
    args = parser.parse_args()

    variants = build_variants(args.decisions)
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
            "tfr",
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
