"""PFR-1: proxy-flow historical replication, spec PFR-1.md v1.0, $0.

Phase A (the gate): compute proxy-F1 -- close-position-weighted per-1m-bar
volume delta (SME spec 5.2 lineage) -- across the tick year, generate
proxy entries with fc_t13's exact rules through the PRODUCTION engine,
and measure the proxy's fidelity to the true fc_t13 develop cohort:
overlap, direction agreement, EV agreement, S-ladder shape, divergence
anatomy.  Pre-registered gate; fail -> Phase B does not run.

Phase B (only on pass): the same proxy pipeline + engine on 2020-01 ->
2024-12 fixtures, exits T13 / S15 / S20 / S30, stops FRACTION-REBASED
(nominal pts / NQ 30250, the SSX-V3 spec-lock price -- the house 2026
reference) so "20 points today" scales with each year's price level.
S-variants are stop overlays on the T13 entry set (MAE-1 machinery:
stops reprice existing entries, they create no frequency).

Shared code, per spec: the proxy synthesizes per-minute {close, vol,
buy, sell} records from 1m OHLCV; everything downstream -- flow_over,
percentile tables, calibration arithmetic (trailing 60 sessions, >= 500
|F1_5| samples, fmean/pstdev volume z), the fc_t13 strategy and the
backtest engine -- is the true pipeline's own code, imported.  What is
measured is the signal difference, not an implementation difference.

Warmup: proxy percentile tables need >= 55 trailing fixture sessions
(the true pipeline's effective warmup: first tradeable decisions file
2025-11-03, 55 sessions in).  Phase A compares only sessions where BOTH
sides could trade (paired-session set, disclosed).  Phase B trading
starts ~2020-03-25 -- the COVID crash months feed the trailing stats
but are not tradeable days; the 2020 row is partial, disclosed.

Costs/fills, identical to the develop run: 5m-close +- 1 tick adverse
entry, $10/RT, 1 NQ; overlay stops fill at distance + 1 tick adverse
(MAE-1 convention); the engine's 0.35%-frac catastrophic stop and 15:55
flatten are active in every run.

Usage:
  uv run python scripts/pfr_study.py --phase a
  uv run python scripts/pfr_study.py --phase b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
from bisect import bisect_left
from collections import deque
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

from nq_agent.backtest import run_backtest
from nq_agent.flow import flow_over, percentile
from nq_agent.strategy.tfr import TickFlowRegime

ET = ZoneInfo("America/New_York")

FIXTURES = Path("var/fixtures/1m")
TRUE_DECISIONS = Path("var/decisions/k3m")
TRUE_JOURNAL = Path("var/tfr/fc_t13/journal")
MAE1 = Path("var/mae1/mae1.json")
FOMC_PATH = Path("config/fomc_dates.json")
OUT = Path("var/pfr")

POINT_VALUE = 20.0
RT_COST = 10.0
TICK = 0.25
SPEC_LOCK_PRICE = 30250.0            # NQ at SSX-V3 spec-lock, 2026-08-17
CAT_FRAC = 0.0035
S_LADDER = (15, 20, 30, 85)          # Phase A nominal ladder vs MAE-1
S_VARIANTS = (15, 20, 30)            # Phase B fraction-rebased overlays
TRAIL_SESS, MIN_TRAIL_SESS = 60, 55
MIN_F1_SAMPLES = 500
PCTS = (55, 60, 65, 70, 75, 80, 85)
TICK_YEAR = (date(2025, 8, 13), date(2026, 8, 12))
HIST = (date(2020, 1, 1), date(2024, 12, 31))
HOUR_BINS = ((575, 630, "09:35-10:30"), (630, 720, "10:30-12:00"),
             (720, 900, "12:00-15:00"))

GATE = {"overlap_hi": 0.70, "overlap_lo": 0.60, "direction": 0.95,
        "ev_tolerance": 0.35, "ladder_peak_region": (15, 30)}
BUFFERS = (2000.0, 4500.0)           # MNQ buffer replay, 2022 spotlight


# ------------------------------------------------------------- proxy minutes


def proxy_minutes(path: Path) -> dict[int, dict]:
    """Per-minute {close, vol, buy, sell} from the 4-print pseudo-tick
    fixture, buy/sell split by the close-position weight (SME spec 5.2):
    where in its range the minute closed decides how much of its volume
    reads as buying vs selling.  Downstream consumers (flow_over, the
    calibration) are the true pipeline's own functions."""
    lines = path.read_text().splitlines()
    if not lines:
        return {}
    first_ts = lines[0].split('"')[3]
    utc = datetime.fromisoformat(first_ts)
    offset = int(
        (utc.astimezone(ET).replace(tzinfo=None) - utc.replace(tzinfo=None)).total_seconds()
        // 60
    )
    out: dict[int, dict] = {}
    cur = -1
    prices: list[float] = []
    vol = 0

    def close_minute() -> None:
        idx = cur - 570 + 1              # ET 09:30 minute -> index 1
        if len(prices) == 4 and 1 <= idx <= 390:
            h, l, c = max(prices), min(prices), prices[-1]
            w = 0.0 if h == l else (c - l) * 2 / (h - l) - 1
            buy = vol * (1 + w) / 2
            out[idx] = {"close": c, "vol": vol, "buy": buy, "sell": vol - buy}

    for line in lines:
        parts = line.split('"')
        ts, price = parts[3], parts[7]
        minute = (int(ts[11:13]) * 60 + int(ts[14:16]) + offset) % 1440
        if minute != cur:
            if cur >= 0:
                close_minute()
            cur, prices, vol = minute, [], 0
        prices.append(float(price))
        vol += int(line.rsplit(": ", 1)[1].rstrip("}"))
    close_minute()
    return out


def session_bars(minutes: dict[int, dict]) -> tuple[list[float], list[float]]:
    """Per-5m-bar (|f1_5|, vol_5m) samples -- the calibration's food, same
    sampling as compute_calibration/fit_regimes."""
    f1_abs: list[float] = []
    vols: list[float] = []
    for end in range(5, 391, 5):
        window = [minutes[i] for i in range(end - 4, end + 1) if i in minutes]
        if not window:
            continue
        f1_abs.append(abs(flow_over(minutes, end, 5)))
        vols.append(sum(m["vol"] for m in window))
    return f1_abs, vols


def build_proxy_decisions(sessions: list[str], out_dir: Path,
                          restrict: set[str] | None) -> dict:
    """Walk-forward proxy decisions files in the k3m shape.  The trailing
    pool accumulates over every fixture session; a file is emitted only
    with >= MIN_TRAIL_SESS trailing sessions and >= MIN_F1_SAMPLES
    (fit_regimes' own rule), and only for `restrict` sessions if given."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pool: deque[tuple[list[float], list[float]]] = deque(maxlen=TRAIL_SESS)
    emitted, skipped_warmup = 0, 0
    for s in sessions:
        minutes = proxy_minutes(FIXTURES / f"{s}.jsonl")
        if not minutes:
            continue
        wanted = restrict is None or s in restrict
        flat_f1 = [v for day, _ in pool for v in day]
        vols = [v for _, day in pool for v in day]
        if wanted and len(pool) >= MIN_TRAIL_SESS and len(flat_f1) >= MIN_F1_SAMPLES:
            q_table = {str(p): percentile(flat_f1, p) for p in PCTS}
            vol_mean, vol_sd = fmean(vols), pstdev(vols)
            bars: dict[str, dict] = {}
            for end in range(5, 391, 5):
                window = [minutes[i] for i in range(end - 4, end + 1) if i in minutes]
                if not window:
                    continue
                vol_5m = sum(m["vol"] for m in window)
                bars[str(end)] = {
                    "close": window[-1]["close"],
                    "f1_2": flow_over(minutes, end, 2),
                    "f1_5": flow_over(minutes, end, 5),
                    "f1_15": flow_over(minutes, end, 15),
                    "z_vol": round(0.0 if vol_sd == 0 else (vol_5m - vol_mean) / vol_sd, 3),
                    "regime": None, "t_af": None, "mahal": None,
                }
            (out_dir / f"{s}.json").write_text(json.dumps({
                "model": "proxy", "mahal_cut": None, "q_f1": q_table,
                "size_cut": 5, "bars": bars,
            }), encoding="utf-8")
            emitted += 1
        elif wanted:
            skipped_warmup += 1
        pool.append(session_bars(minutes))
    return {"emitted": emitted, "skipped_warmup": skipped_warmup}


# ------------------------------------------------------------- engine replay


def base_config(out_root: Path) -> Path:
    config = out_root / "pfr-base.yaml"
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


async def run_fc_t13(decisions_dir: Path, start: date, end: date, run_dir: Path):
    decisions = {p.stem: json.loads(p.read_text(encoding="utf-8"))
                 for p in sorted(decisions_dir.glob("*.json"))}
    fomc = {date.fromisoformat(d)
            for d in json.loads(FOMC_PATH.read_text(encoding="utf-8"))["dates"]}
    factory = partial(TickFlowRegime, decisions=decisions, fomc_dates=fomc,
                      exit_mode="t13", regime_required=False)
    fixtures = [p for p in sorted(FIXTURES.glob("*.jsonl"))
                if start <= date.fromisoformat(p.stem) <= end]
    if run_dir.exists():
        shutil.rmtree(run_dir)                 # fresh journal, no stale trades
    run_dir.mkdir(parents=True)
    config = base_config(run_dir)
    return await run_backtest(config, fixtures, "tfr", run_dir / "fc_t13",
                              strategy_factory=factory)  # type: ignore[arg-type]


def load_journal(journal_dir: Path) -> list[dict]:
    trades: list[dict] = []
    for path in sorted(journal_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if '"position_closed"' not in line:
                continue
            r = json.loads(line)
            if r.get("event") != "position_closed":
                continue
            et_in = datetime.fromisoformat(r["entry_time"]).astimezone(ET)
            et_out = datetime.fromisoformat(r["exit_time"]).astimezone(ET)
            trades.append({
                "session": et_in.date().isoformat(),
                "m_in": et_in.hour * 60 + et_in.minute,
                "m_out": et_out.hour * 60 + et_out.minute,
                "dir": 1 if r["direction"] == "LONG" else -1,
                "qty": int(r.get("quantity", 1)),
                "fill": float(r["entry_price"]),
                "exit_reason": r.get("exit_reason"),
                "pnl": float(r["realised_pnl"]),
            })
    return trades


# ---------------------------------------------------------- MAE + overlays


def load_ohlc(path: Path) -> tuple[list[int], list[tuple]]:
    """Per-minute (o, h, l, c) minute-of-day keyed (MAE walk convention)."""
    lines = path.read_text().splitlines()
    if not lines:
        return [], []
    first_ts = lines[0].split('"')[3]
    utc = datetime.fromisoformat(first_ts)
    offset = int(
        (utc.astimezone(ET).replace(tzinfo=None) - utc.replace(tzinfo=None)).total_seconds()
        // 60
    )
    minutes: list[int] = []
    bars: list[tuple] = []
    cur = -1
    prices: list[float] = []
    for line in lines:
        parts = line.split('"')
        minute = (int(parts[3][11:13]) * 60 + int(parts[3][14:16]) + offset) % 1440
        if minute != cur:
            if len(prices) == 4:
                minutes.append(cur)
                bars.append(tuple(prices))
            cur, prices = minute, []
        prices.append(float(parts[7]))
    if len(prices) == 4:
        minutes.append(cur)
        bars.append(tuple(prices))
    return minutes, bars


def attach_mae(trades: list[dict]) -> None:
    cache: dict[str, tuple] = {}
    for t in trades:
        if t["session"] not in cache:
            cache[t["session"]] = load_ohlc(FIXTURES / f"{t['session']}.jsonl")
        minutes, bars = cache[t["session"]]
        i0 = bisect_left(minutes, t["m_in"])
        i1 = bisect_left(minutes, t["m_out"])
        mae = 0.0
        for i in range(i0, i1):
            _, h, l, _ = bars[i]
            adv = t["fill"] - l if t["dir"] == 1 else h - t["fill"]
            mae = max(mae, adv)
        t["mae"] = round(mae, 2)


def overlay(trades: list[dict], stop_pts: float | None = None,
            stop_frac: float | None = None) -> list[dict]:
    """Stop-at-distance repricing of an existing trade set (MAE-1 method:
    touch -> fill at distance + 1 tick adverse; unstopped keep engine P&L).
    stop_pts = nominal (Phase A ladder); stop_frac = fraction of fill
    (Phase B rebased)."""
    out = []
    for t in trades:
        dist = stop_pts if stop_pts is not None else stop_frac * t["fill"]
        if t["mae"] >= dist:
            pnl = -((dist + TICK) * POINT_VALUE + RT_COST) * t["qty"]
            stopped = True
        else:
            pnl, stopped = t["pnl"], False
        out.append({**t, "sim_pnl": pnl, "stopped": stopped,
                    "risk_usd": (dist + TICK) * POINT_VALUE})
    return out


def panel(rows: list[dict], months_in_span: list[str]) -> dict:
    if not rows:
        return {"n": 0}
    pnls = [r["sim_pnl"] for r in rows]
    per_day: dict[str, float] = {}
    for r in rows:
        per_day[r["session"]] = per_day.get(r["session"], 0.0) + r["sim_pnl"]
    days = sorted(per_day)
    worst5 = (min(sum(per_day[d] for d in days[i:i + 5])
                  for i in range(len(days) - 4)) if len(days) >= 5
              else sum(per_day.values()))
    equity = peak = dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    per_month = {m: 0 for m in months_in_span}
    for r in rows:
        per_month[r["session"][:7]] += 1
    return {
        "n": len(rows), "stopped": sum(1 for r in rows if r["stopped"]),
        "ev_usd": round(statistics.mean(pnls), 2),
        "net_usd": round(sum(pnls), 2),
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4),
        "pf": round(gw / gl, 3) if gl else None,
        "trades_per_month_median": statistics.median(per_month.values()),
        "worst_5day_usd": round(worst5, 2),
        "max_dd_usd": round(dd, 2),
        "ev_per_dollar_risked": round(
            statistics.mean(pnls) / statistics.mean(r["risk_usd"] for r in rows), 4),
    }


def months_of(year: str, rows: list[dict]) -> list[str]:
    ms = sorted({r["session"][:7] for r in rows if r["session"].startswith(year)})
    return ms or [f"{year}-01"]


# ---------------------------------------------------------------- phase A


def hour_bin(m: int) -> str:
    for i, (lo, hi, lbl) in enumerate(HOUR_BINS):
        if (lo <= m if i == 0 else lo < m) and m <= hi:
            return lbl
    return "other"


def entry_z_vol(t: dict, decisions_dir: Path, cache: dict) -> float | None:
    if t["session"] not in cache:
        p = decisions_dir / f"{t['session']}.json"
        cache[t["session"]] = json.loads(p.read_text()).get("bars", {}) if p.exists() else {}
    rec = cache[t["session"]].get(str(t["m_in"] - 570))
    return rec.get("z_vol") if isinstance(rec, dict) else None


def anatomy(rows: list[dict], decisions_dir: Path, cache: dict) -> dict:
    zs = [z for t in rows if (z := entry_z_vol(t, decisions_dir, cache)) is not None]
    return {
        "n": len(rows),
        "by_hour": {lbl: sum(1 for t in rows if hour_bin(t["m_in"]) == lbl)
                    for lbl in [b[2] for b in HOUR_BINS] + ["other"]},
        "z_vol_mean": round(statistics.mean(zs), 3) if zs else None,
        "z_vol_ge_1_share": round(sum(1 for z in zs if z >= 1) / len(zs), 3) if zs else None,
    }


async def phase_a() -> None:
    sessions = sorted(p.stem for p in FIXTURES.glob("*.jsonl")
                      if TICK_YEAR[0].isoformat() <= p.stem <= TICK_YEAR[1].isoformat())
    true_tradeable = set()
    for p in sorted(TRUE_DECISIONS.glob("*.json")):
        d = json.loads(p.read_text())
        if d.get("model") is not None and d.get("q_f1"):
            true_tradeable.add(p.stem)
    print(f"{len(sessions)} tick-year sessions, {len(true_tradeable)} true-tradeable")

    dec_dir = OUT / "decisions-a"
    stats = build_proxy_decisions(sessions, dec_dir, restrict=true_tradeable)
    paired = {p.stem for p in dec_dir.glob("*.json")} & true_tradeable
    print(f"proxy decisions: {stats}, paired sessions: {len(paired)}")

    report = await run_fc_t13(dec_dir, *TICK_YEAR, OUT / "a")
    print(report.render())

    proxy = [t for t in load_journal(OUT / "a" / "fc_t13" / "journal")
             if t["session"] in paired]
    true = [t for t in load_journal(TRUE_JOURNAL) if t["session"] in paired]
    print(f"cohorts within paired sessions: true {len(true)}, proxy {len(proxy)}")

    # 1-2. overlap (same 5m bar +-1, greedy one-to-one) + direction agreement
    matched: list[tuple[dict, dict]] = []
    free = list(proxy)
    for tt in true:
        best = None
        for pt in free:
            if pt["session"] == tt["session"] and abs(pt["m_in"] - tt["m_in"]) <= 5:
                if best is None or abs(pt["m_in"] - tt["m_in"]) < abs(best["m_in"] - tt["m_in"]):
                    best = pt
        if best is not None:
            matched.append((tt, best))
            free.remove(best)
    dir_agree = (sum(1 for tt, pt in matched if tt["dir"] == pt["dir"]) / len(matched)
                 if matched else 0.0)

    # 3. EV agreement + S-ladder shape (nominal points, MAE-1 convention)
    attach_mae(proxy)
    ev_true = statistics.mean(t["pnl"] for t in true) if true else 0.0
    ev_proxy = statistics.mean(t["pnl"] for t in proxy) if proxy else 0.0
    span = sorted({t["session"][:7] for t in proxy})
    ladder = {f"S{s}": panel(overlay(proxy, stop_pts=s), span) for s in S_LADDER}
    peak_s = max(S_LADDER, key=lambda s: ladder[f"S{s}"].get("ev_per_dollar_risked", -9e9))
    mae1 = json.loads(MAE1.read_text()) if MAE1.exists() else None
    true_ladder = ({f"S{s}": {k: mae1["unconditional"][f"S{s}"][k]
                              for k in ("ev_usd", "pf", "win_rate", "ev_per_dollar_risked")}
                    for s in S_LADDER} if mae1 else None)

    # 4. divergence anatomy
    matched_true_ids = {id(tt) for tt, _ in matched}
    matched_proxy_ids = {id(pt) for _, pt in matched}
    zc: dict = {}
    diverg = {
        "matched": anatomy([tt for tt, _ in matched], TRUE_DECISIONS, zc),
        "true_only": anatomy([t for t in true if id(t) not in matched_true_ids],
                             TRUE_DECISIONS, zc),
        "proxy_only": anatomy([t for t in proxy if id(t) not in matched_proxy_ids],
                              OUT / "decisions-a", {}),
    }

    overlap_true = len(matched) / len(true) if true else 0.0
    overlap_proxy = len(matched) / len(proxy) if proxy else 0.0
    gate = {
        "overlap": (max(overlap_true, overlap_proxy) >= GATE["overlap_hi"]
                    and min(overlap_true, overlap_proxy) >= GATE["overlap_lo"]),
        "direction": dir_agree >= GATE["direction"],
        "ev": abs(ev_proxy - ev_true) <= GATE["ev_tolerance"] * abs(ev_true)
              if ev_true else False,
        "ladder_ordering": GATE["ladder_peak_region"][0] <= peak_s
                           <= GATE["ladder_peak_region"][1],
    }
    gate["pass"] = all(gate.values())

    out = {
        "meta": {"run_date": "2026-08-21", "spec": "PFR-1 v1.0 Phase A",
                 "paired_sessions": len(paired),
                 "proxy_decisions": stats,
                 "params": {"trail": [TRAIL_SESS, MIN_TRAIL_SESS],
                            "min_f1_samples": MIN_F1_SAMPLES,
                            "gate": GATE, "s_ladder": list(S_LADDER)}},
        "counts": {"true": len(true), "proxy": len(proxy),
                   "matched": len(matched),
                   "count_ratio_proxy_over_true": round(len(proxy) / len(true), 3)
                   if true else None},
        "overlap": {"true_matched_share": round(overlap_true, 4),
                    "proxy_matched_share": round(overlap_proxy, 4)},
        "direction_agreement": round(dir_agree, 4),
        "ev": {"true": round(ev_true, 2), "proxy": round(ev_proxy, 2),
               "ratio": round(ev_proxy / ev_true, 3) if ev_true else None},
        "proxy_s_ladder": ladder,
        "true_s_ladder_mae1": true_ladder,
        "ladder_peak_s": peak_s,
        "divergence_anatomy": diverg,
        "gate": gate,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "calibration.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    with (OUT / "a" / "trades.jsonl").open("w", encoding="utf-8") as fh:
        for t in proxy:
            fh.write(json.dumps(t) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "proxy_s_ladder"}, indent=1))
    print("proxy ladder (ev/$risked): "
          + ", ".join(f"S{s}={ladder[f'S{s}'].get('ev_per_dollar_risked')}"
                      for s in S_LADDER))
    print(f"\nGATE: {'PASS' if gate['pass'] else 'FAIL'} -> "
          f"{'Phase B may run' if gate['pass'] else 'Phase B does not run; the true-tick purchase is the honest route'}")
    print(f"written -> {OUT / 'calibration.json'}")


# ---------------------------------------------------------------- phase B


async def phase_b() -> None:
    cal_path = OUT / "calibration.json"
    if not cal_path.exists():
        raise SystemExit("Phase A has not run; run --phase a first.")
    cal = json.loads(cal_path.read_text())
    if not cal["gate"]["pass"]:
        raise SystemExit("Phase A gate FAILED; per spec, Phase B does not run.")

    sessions = sorted(p.stem for p in FIXTURES.glob("*.jsonl")
                      if HIST[0].isoformat() <= p.stem <= HIST[1].isoformat())
    print(f"{len(sessions)} historical sessions {sessions[0]}..{sessions[-1]}")
    dec_dir = OUT / "decisions-b"
    stats = build_proxy_decisions(sessions, dec_dir, restrict=None)
    print(f"proxy decisions: {stats}")

    report = await run_fc_t13(dec_dir, *HIST, OUT / "b")
    print(report.render())
    trades = load_journal(OUT / "b" / "fc_t13" / "journal")
    print(f"{len(trades)} T13 trades; attaching MAE for overlays...")
    attach_mae(trades)

    years = [str(y) for y in range(HIST[0].year, HIST[1].year + 1)]
    variants: dict[str, list[dict]] = {
        "T13": [{**t, "sim_pnl": t["pnl"], "stopped": False,
                 "risk_usd": (CAT_FRAC * t["fill"] + TICK) * POINT_VALUE}
                for t in trades],
    }
    fracs = {}
    for s in S_VARIANTS:
        frac = s / SPEC_LOCK_PRICE
        fracs[f"S{s}"] = round(frac, 6)
        variants[f"S{s}"] = overlay(trades, stop_frac=frac)

    looks = 0
    by_year: dict[str, dict] = {}
    for name, rows in variants.items():
        by_year[name] = {}
        for y in years:
            yr = [r for r in rows if r["session"].startswith(y)]
            by_year[name][y] = panel(yr, months_of(y, rows))
            looks += 1
        by_year[name]["all"] = panel(rows, sorted({r["session"][:7] for r in rows}))

    # 2022-bear spotlight: streaks + MNQ buffer replay for the tight shapes
    spotlight: dict[str, dict] = {}
    for name in [f"S{s}" for s in S_VARIANTS]:
        rows = [r for r in variants[name] if r["session"].startswith("2022")]
        streaks, run = [], 0
        for r in rows:
            if r["sim_pnl"] < 0:
                run += 1
            elif run:
                streaks.append(run)
                run = 0
        if run:
            streaks.append(run)
        replay = {}
        for b in BUFFERS:
            equity = peak = 0.0
            busts = 0
            for r in rows:
                equity += r["sim_pnl"] / 10.0          # 1 MNQ
                peak = max(peak, equity)
                if peak - equity >= b:
                    busts += 1
                    equity = peak = 0.0                # fresh buffer
            replay[f"${int(b)}"] = {"busts": busts,
                                    "end_equity_mnq": round(equity, 2)}
        spotlight[name] = {
            "n_2022": len(rows),
            "losing_streaks": {"max": max(streaks, default=0),
                               "mean": round(statistics.mean(streaks), 2) if streaks else 0,
                               "dist": {str(k): streaks.count(k)
                                        for k in sorted(set(streaks))}},
            "buffer_replay_mnq": replay,
        }

    out = {
        "meta": {"run_date": "2026-08-21", "spec": "PFR-1 v1.0 Phase B",
                 "grade": "proxy-grade, calibrated -- Phase A fidelity attached; "
                          "NEVER quoted as fc_t13 results",
                 "phase_a_fidelity": {"overlap": cal["overlap"],
                                      "direction_agreement": cal["direction_agreement"],
                                      "ev_ratio": cal["ev"]["ratio"]},
                 "proxy_decisions": stats,
                 "stop_fracs_of_price": fracs,
                 "rebase_reference": SPEC_LOCK_PRICE,
                 "warmup_note": "trading starts after 55 trailing sessions "
                                "(~2020-03-25); COVID crash feeds stats, not trades; "
                                "2020 row is partial",
                 "looks": looks},
        "by_variant_by_year": by_year,
        "spotlight_2022": spotlight,
    }
    (OUT / "historical.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    with (OUT / "b" / "trades.jsonl").open("w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t) + "\n")

    for name in variants:
        print(f"\n===== {name} =====")
        for y in years + ["all"]:
            p = by_year[name][y]
            if p["n"] == 0:
                print(f"{y}: no trades")
                continue
            print(f"{y}: n={p['n']:>4} ev ${p['ev_usd']:>8.2f} wr {p['win_rate']:.3f} "
                  f"pf {p['pf'] or 0:>6.3f} tpm {p['trades_per_month_median']:>4} "
                  f"worst5 ${p['worst_5day_usd']:>9.2f} maxDD ${p['max_dd_usd']:>9.2f} "
                  f"ev/$ {p['ev_per_dollar_risked']:>7.4f}")
    print(json.dumps(spotlight, indent=1))
    print(f"\nlooks: {looks} variant-year panels + calibration")
    print(f"written -> {OUT / 'historical.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("a", "b"), required=True)
    args = parser.parse_args()
    asyncio.run(phase_a() if args.phase == "a" else phase_b())


if __name__ == "__main__":
    main()
