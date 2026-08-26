"""VPC Phase 1 -- VWAP Pullback Continuation (spec v1.0, program 10).

The $0 develop measurement on existing data: flow-confirmed trend ->
pullback to session VWAP -> quiet-or-absorbed counter-flow -> enter with
the trend. Frequency reality check FIRST (spec section 5): if qualifying
setups run under 8/month at defaults, record and stop -- the P&L matrix
is not even computed, because rare-but-real is shelved, never force-fed.

Phase-1 deviations from spec section 10, each declared:

- Study-level simulation, not an engine module. strategy/vpc.py and the
  decision-file precompute are the post-gate steps; var/decisions/k3m
  feeds the LIVE fc_t13 shadow and is not touched mid-shadow.
- VWAP from the 1m pseudo-tick fixtures: typical price (H+L+C)/3
  weighted by the minute volume the fixture carries on its close print,
  anchored 09:30, consulted strictly from completed minutes.
- The quiet condition needs Q50 of |F1_5|; the decision tables start at
  Q55. Q50 is computed here as a walk-forward trailing-60-session
  quantile of the same |F1_5| population (>= 30 prior sessions
  required), not read from a table.
- Fills: entry at next 1m open + 1 tick adverse (house). Catastrophic
  stop (0.35 percent, fraction) stop-market + 1 tick adverse,
  adverse-first inside every 1m bar. Targets are limits at touch.
  Thesis-death and time exits are market-on-5m-close, filled at the
  close -- the engine's own convention for reactive exits (fc_t13's
  journal EV is built on it), so the incumbent comparison is
  like-for-like.

Usage:
  uv run python scripts/run_vpc.py [--out var/vpc] [--force]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FIX_1M = Path("var/fixtures/1m")
DECISIONS = Path("var/decisions/k3m")
FOMC = Path("config/fomc_dates.json")
FC_JOURNAL = Path("var/tfr/fc_t13/journal")

POINT_VALUE = 20.0
RT_COST = 10.0
TICK = 0.25
P_REF = 30250.0                     # spec-lock reference (V-30 fraction)
CAT_F = 0.0035                      # house catastrophic stop
TGT30_F = 30.0 / P_REF

# spec section 2/3 defaults (sweeps recorded separately)
N_TREND = 6                         # impulse must be within 6 five-min bars
P_EXT = 0.0010                      # 0.10 percent extension
P_TOUCH = 0.0002                    # 0.02 percent touch zone
ENTRY_FIRST = 585                   # 09:45 ET, minutes from midnight (5m close)
ENTRY_LAST = 870                    # 14:30 ET
FOMC_CUTOFF = 780                   # 13:00 ET
FLATTEN_MIN = 954                   # close of this 1m bar = the 15:55 flatten
CAP = 2
T13 = 13
Q50_TRAIL = 60
Q50_MIN_SESSIONS = 30

VARIANTS = ("v_t13", "v_imp", "v_30", "v_imp_h")
STOP_MODES = ("flow", "any")


# ---------------------------------------------------------------- loaders


def load_bars(session: str) -> tuple[list[int], list[tuple]]:
    """Per-minute (o, h, l, c, v) from the 4-print pseudo-tick fixture."""
    lines = (FIX_1M / f"{session}.jsonl").read_text().splitlines()
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
    cur_min = -1
    prices: list[float] = []
    vol = 0
    for line in lines:
        parts = line.split('"')
        record = json.loads(line)
        ts = parts[3]
        minute = (int(ts[11:13]) * 60 + int(ts[14:16]) + offset) % 1440
        if minute != cur_min:
            if len(prices) == 4:
                minutes.append(cur_min)
                bars.append((*prices, vol))
            cur_min = minute
            prices = []
        prices.append(float(record["price"]))
        vol = record["size"]
    if len(prices) == 4:
        minutes.append(cur_min)
        bars.append((*prices, vol))
    return minutes, bars


def vwap_series(minutes: list[int], bars: list[tuple]) -> dict[int, float]:
    """VWAP anchored 09:30, keyed by minute; value includes that minute's bar.

    Consulted at a 5m close M as vwap[M-1]: the last COMPLETED minute.
    """
    out: dict[int, float] = {}
    pv = 0.0
    v = 0.0
    for m, (o, h, l, c, vol) in zip(minutes, bars):
        if m < 570:
            continue
        typical = (h + l + c) / 3.0
        pv += typical * vol
        v += vol
        if v > 0:
            out[m] = pv / v
    return out


def sessions_universe() -> list[str]:
    """Sessions with a fitted model AND fixture coverage -- fc_t13's set."""
    out = []
    for path in sorted(DECISIONS.glob("*.json")):
        day = json.loads(path.read_text())
        if day.get("model") is None:
            continue
        if (FIX_1M / f"{path.stem}.jsonl").exists():
            out.append(path.stem)
    return out


def load_day(session: str) -> dict:
    return json.loads((DECISIONS / f"{session}.json").read_text())


def trailing_q50() -> dict[str, float]:
    """Walk-forward trailing-60-session Q50 of |F1_5|, per session."""
    pools: list[list[float]] = []
    out: dict[str, float] = {}
    for path in sorted(DECISIONS.glob("*.json")):
        day = json.loads(path.read_text())
        if len(pools) >= Q50_MIN_SESSIONS:
            flat = sorted(x for pool in pools[-Q50_TRAIL:] for x in pool)
            if flat:
                out[path.stem] = flat[len(flat) // 2]
        values = [
            abs(rec["f1_5"])
            for rec in day.get("bars", {}).values()
            if isinstance(rec, dict) and rec.get("f1_5") is not None
        ]
        if values:
            pools.append(values)
    return out


def fomc_dates() -> set[str]:
    payload = json.loads(FOMC.read_text())
    return set(payload["dates"])


# ---------------------------------------------------------------- detection


def detect_setups(
    session: str,
    day: dict,
    minutes: list[int],
    bars: list[tuple],
    vwap: dict[int, float],
    q50: float | None,
    fomc: bool,
    trend_override: int | None = None,
    require_flow: bool = True,
    f1_signs: dict[int, int] | None = None,
    funnel: dict | None = None,
) -> list[dict]:
    """All qualifying pullback setups for one session (pre-cap).

    trend_override: fixed direction for the trend-prev control.
    require_flow=False: the naive-pullback control (any holding pullback).
    f1_signs: per-index sign flips for the permutation control.
    funnel: optional counter dict; attrition stages are tallied into it so
    the frequency verdict states WHERE setups die, not just how many live.
    """

    def tally(stage: str) -> None:
        if funnel is not None:
            funnel[stage] = funnel.get(stage, 0) + 1
    table = day.get("q_f1") or {}
    if "70" not in table or q50 is None:
        return []
    q70 = float(table["70"])
    day_bars = day.get("bars", {})

    setups: list[dict] = []
    trend_dir = 0
    last_impulse = -999
    last_ext = None          # 5m index of the last bar with dist >= P_EXT
    trend_start = 0
    flows: dict[int, float] = {}   # 5m index -> signed f1_5 (post-permutation)
    effs: dict[int, float] = {}

    for i in range(5, 391, 5):
        rec = day_bars.get(str(i))
        if not isinstance(rec, dict) or rec.get("f1_5") is None:
            continue
        f1 = rec["f1_5"]
        if f1_signs is not None:
            f1 = abs(f1) * f1_signs.get(i, 1)
        flows[i] = f1
        if rec.get("z_eff") is not None:
            effs[i] = rec["z_eff"]
        close_minute = 570 + i
        vw = vwap.get(close_minute - 1)
        close = rec.get("close")
        if vw is None or close is None:
            continue

        if trend_override is not None:
            trend_dir = trend_override
            live = True
            if last_ext is None and trend_start == 0:
                trend_start = i
        else:
            if abs(f1) >= q70 and f1 != 0:
                sign = 1 if f1 > 0 else -1
                if sign != trend_dir:
                    trend_dir = sign
                    trend_start = i
                    last_ext = None
                last_impulse = i
            live = trend_dir != 0 and (i - last_impulse) <= N_TREND * 5

        if trend_dir == 0:
            continue
        dist = trend_dir * (close - vw) / close
        if dist >= P_EXT:
            if last_ext is None:
                tally("extension_legs")
            last_ext = i
            continue
        if last_ext is None:
            continue
        if dist > P_TOUCH:
            continue
        tally("touches")
        if not live:
            tally("touch_but_trend_expired")
            continue
        hold = trend_dir * (close - vw) > 0
        if not hold:
            tally("touch_no_hold")
            continue
        tally("held_pullbacks")

        # classify the pullback bars (after the last extended bar, incl. now)
        pull = [j for j in range(last_ext + 5, i + 5, 5) if j in flows]
        counter = [j for j in pull if flows[j] * trend_dir < 0]
        quiet = all(abs(flows[j]) < q50 for j in counter)
        absorbed = any(
            abs(flows[j]) >= q70 and effs.get(j, 0.0) <= -1.0 for j in counter
        )
        if require_flow and not (quiet or absorbed):
            tally("fail_flow_condition")
            continue
        if not (ENTRY_FIRST <= close_minute <= ENTRY_LAST):
            tally("out_of_window")
            continue
        if fomc and close_minute > FOMC_CUTOFF:
            tally("fomc_block")
            continue
        tally("setups")

        # impulse extreme: the swing the trend made before this pullback,
        # from completed 1m bars between trend start and the signal close
        lo = bisect_left(minutes, 570 + trend_start - 5)
        hi = bisect_right(minutes, close_minute - 1)
        if trend_dir == 1:
            swing = max((bars[k][1] for k in range(lo, hi)), default=None)
        else:
            swing = min((bars[k][2] for k in range(lo, hi)), default=None)

        setups.append(
            {
                "session": session,
                "index": i,
                "minute": close_minute,
                "dir": trend_dir,
                "close": close,
                "vwap": vw,
                "flavor": "absorbed" if absorbed else "quiet",
                "swing": swing,
            }
        )
        last_ext = None  # one entry per pullback leg; a new extension re-arms
    return setups


# ---------------------------------------------------------------- simulate


def simulate_cell(
    per_session: dict[str, list[dict]],
    data: dict[str, tuple],
    variant: str,
    stop_mode: str,
    cap: int = CAP,
) -> list[dict]:
    """Sequential per-session simulation of one (variant, stop mode) cell."""
    trades: list[dict] = []
    for session, setups in per_session.items():
        minutes, bars, vwap, day, q70 = data[session]
        day_bars = day.get("bars", {})
        entries = 0
        busy_until = -1  # minute; a position blocks later signals
        for setup in setups:
            if entries >= cap:
                break
            # strictly later 5m close than the exit bar: no same-bar re-entry
            if setup["minute"] <= busy_until + 1:
                continue
            trade = _run_trade(setup, minutes, bars, vwap, day_bars, q70, variant, stop_mode)
            if trade is None:
                continue
            entries += 1
            busy_until = trade["exit_minute"]
            trades.append(trade)
    return trades


def _run_trade(
    setup: dict,
    minutes: list[int],
    bars: list[tuple],
    vwap: dict[int, float],
    day_bars: dict,
    q70: float,
    variant: str,
    stop_mode: str,
) -> dict | None:
    d = setup["dir"]
    m0 = setup["minute"]              # entry 5m close; fill at this minute's open
    i0 = bisect_left(minutes, m0)
    if i0 >= len(minutes) or minutes[i0] != m0:
        return None
    entry = bars[i0][0] + d * TICK
    cstop = entry * (1 - d * CAT_F)
    target = None
    if variant in ("v_imp", "v_imp_h") and setup["swing"] is not None:
        if d * (setup["swing"] - entry) > TICK:
            target = setup["swing"]
    if variant == "v_30":
        target = entry * (1 + d * TGT30_F)

    exit_px = exit_reason = exit_minute = None
    for k in range(i0, len(minutes)):
        m = minutes[k]
        o, h, l, c, _ = bars[k]
        lo_hit = l <= cstop if d == 1 else h >= cstop
        if lo_hit:
            gapped = o < cstop if d == 1 else o > cstop
            exit_px = o if gapped else cstop - d * TICK
            exit_reason, exit_minute = "cat_stop", m
            break
        if target is not None and (h >= target if d == 1 else l <= target):
            exit_px, exit_reason, exit_minute = target, "target", m
            break
        if m >= FLATTEN_MIN:
            exit_px, exit_reason, exit_minute = c, "flatten", m
            break
        # 5m close checks once the block ending at m+1 is complete
        close_minute = m + 1
        if (close_minute - 570) % 5 == 0 and close_minute > m0:
            held = (close_minute - m0) // 5
            index = close_minute - 570
            vw = vwap.get(m)
            through = vw is not None and d * (c - vw) < 0
            if through:
                rec = day_bars.get(str(index))
                f1 = rec.get("f1_5") if isinstance(rec, dict) else None
                initiative = f1 is not None and f1 * d < 0 and abs(f1) >= q70
                if stop_mode == "any" or initiative:
                    exit_px, exit_reason, exit_minute = c, "vwap_stop", m
                    break
            if variant in ("v_t13", "v_imp_h") and held >= T13:
                exit_px, exit_reason, exit_minute = c, "time_13", m
                break
    if exit_px is None:
        exit_px, exit_reason, exit_minute = bars[-1][3], "flatten", minutes[-1]
    pnl = d * (exit_px - entry) * POINT_VALUE - RT_COST
    return {
        **{k: setup[k] for k in ("session", "dir", "flavor")},
        "entry": entry,
        "exit": exit_px,
        "exit_reason": exit_reason,
        "exit_minute": exit_minute,
        "minutes_held": exit_minute - m0 + 1,
        "pnl": round(pnl, 2),
    }


# ---------------------------------------------------------------- panels


def t_stat(values: list[float]) -> float:
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    sd = statistics.stdev(values)
    return mean / (sd / math.sqrt(n)) if sd else 0.0


def panel(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    reasons: dict[str, int] = defaultdict(int)
    for t in trades:
        reasons[t["exit_reason"]] += 1
    monthly: dict[str, float] = defaultdict(float)
    for t in trades:
        monthly[t["session"][:7]] += t["pnl"]
    quarters: dict[str, float] = defaultdict(float)
    for t in trades:
        y, m = int(t["session"][:4]), int(t["session"][5:7])
        quarters[f"{y}Q{(m - 1) // 3 + 1}"] += t["pnl"]
    return {
        "n": n,
        "ev": round(statistics.mean(pnls), 2),
        "total": round(sum(pnls), 2),
        "t": round(t_stat(pnls), 2),
        "win_rate": round(len(wins) / n, 4),
        "pf": round(sum(wins) / -sum(losses), 3) if losses else None,
        "by_exit": dict(reasons),
        "quarters_positive": f"{sum(v > 0 for v in quarters.values())}/{len(quarters)}",
        "neg_months": sum(v < 0 for v in monthly.values()),
    }


def frequency_panel(per_session: dict[str, list[dict]]) -> dict:
    monthly: dict[str, int] = defaultdict(int)
    for session, setups in per_session.items():
        monthly[session[:7]] += len(setups)
    counts = [monthly[m] for m in sorted(monthly)]
    total = sum(counts)
    return {
        "total_setups": total,
        "per_month": {m: monthly[m] for m in sorted(monthly)},
        "monthly_median": statistics.median(counts) if counts else 0,
        "monthly_min": min(counts) if counts else 0,
    }


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("var/vpc"))
    parser.add_argument(
        "--force", action="store_true",
        help="compute the P&L matrix even if the frequency gate fails",
    )
    parser.add_argument("--permutations", type=int, default=100)
    args = parser.parse_args()

    sessions = sessions_universe()
    q50_by_session = trailing_q50()
    fomc = fomc_dates()
    print(f"{len(sessions)} sessions with fitted model + fixtures")

    data: dict[str, tuple] = {}
    per_session: dict[str, list[dict]] = {}
    funnel_counts: dict[str, int] = {}
    sweeps: dict[str, dict[str, int]] = defaultdict(dict)
    global N_TREND, P_EXT
    for session in sessions:
        minutes, bars = load_bars(session)
        vwap = vwap_series(minutes, bars)
        day = load_day(session)
        q70 = float((day.get("q_f1") or {}).get("70", "nan"))
        data[session] = (minutes, bars, vwap, day, q70)
        base_args = (session, day, minutes, bars, vwap,
                     q50_by_session.get(session), session in fomc)
        per_session[session] = detect_setups(*base_args, funnel=funnel_counts)
        # pre-declared sweeps, frequency only
        for label, (nt, pe) in {
            "n_trend_12": (12, P_EXT), "p_ext_008": (6, 0.0008), "p_ext_015": (6, 0.0015),
        }.items():
            N_TREND, P_EXT = nt, pe
            sweeps[label][session] = len(detect_setups(*base_args))
            N_TREND, P_EXT = 6, 0.0010

    freq = frequency_panel(per_session)
    sweep_freq = {}
    for label, counts in sweeps.items():
        monthly: dict[str, int] = defaultdict(int)
        for session, n in counts.items():
            monthly[session[:7]] += n
        vals = [monthly[m] for m in sorted(monthly)]
        sweep_freq[label] = {
            "total": sum(vals),
            "monthly_median": statistics.median(vals) if vals else 0,
        }

    print("\n=== frequency reality check (the gate that runs first) ===")
    print(f"defaults: {freq}")
    print(f"funnel: {funnel_counts}")
    for label, f in sweep_freq.items():
        print(f"  sweep {label}: {f}")

    gate_pass = freq["monthly_median"] >= 8
    result: dict = {
        "spec": "VPC v1.0 Phase 1",
        "sessions": len(sessions),
        "frequency": freq,
        "funnel": funnel_counts,
        "frequency_sweeps": sweep_freq,
        "frequency_gate": "PASS" if gate_pass else "FAIL (< 8 setups/month median)",
    }

    if not gate_pass and not args.force:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "phase1.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print("\nFrequency gate FAILED -- recording and stopping per spec section 5.")
        print(f"written -> {args.out / 'phase1.json'}")
        return

    # ---- the exit matrix ----
    print("\n=== exit matrix ===")
    cells: dict[str, dict] = {}
    trades_by_cell: dict[str, list[dict]] = {}
    for variant in VARIANTS:
        for stop_mode in STOP_MODES:
            key = f"{variant}|{stop_mode}"
            trades = simulate_cell(per_session, data, variant, stop_mode)
            trades_by_cell[key] = trades
            cells[key] = panel(trades)
            print(f"  {key}: {cells[key]}")
    result["cells"] = cells

    # headline: highest develop EV among flow-stop variants (pre-registered)
    headline_key = max(
        (f"{v}|flow" for v in VARIANTS), key=lambda k: cells[k].get("ev", -1e9)
    )
    headline = trades_by_cell[headline_key]
    result["headline_cell"] = headline_key
    print(f"\nheadline cell: {headline_key}")

    # ---- companions ----
    flavor = {
        fl: panel([t for t in headline if t["flavor"] == fl])
        for fl in ("quiet", "absorbed")
    }
    result["flavor_split"] = flavor
    print(f"quiet vs absorbed: {flavor}")

    stop_pnls = defaultdict(list)
    for t in headline:
        stop_pnls[t["exit_reason"]].append(t["pnl"])
    result["stop_anatomy"] = {
        r: {"n": len(v), "avg": round(statistics.mean(v), 2)} for r, v in stop_pnls.items()
    }
    print(f"stop anatomy: {result['stop_anatomy']}")

    # fc_t13 same-session comparison
    fc: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(FC_JOURNAL.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if '"position_closed"' not in line:
                continue
            r = json.loads(line)
            if r.get("event") != "position_closed":
                continue
            entry_et = datetime.fromisoformat(r["entry_time"]).astimezone(ET)
            fc[entry_et.date().isoformat()].append(
                {"dir": 1 if r["direction"] == "LONG" else -1,
                 "entry": float(r["entry_price"]), "pnl": float(r["realised_pnl"])}
            )
    improvements, vpc_pnl_overlap, fc_pnl_overlap = [], [], []
    overlap_sessions = set()
    for t in headline:
        for f in fc.get(t["session"], []):
            if f["dir"] == t["dir"]:
                improvements.append(t["dir"] * (f["entry"] - t["entry"]))
                overlap_sessions.add(t["session"])
                break
    for s in overlap_sessions:
        vpc_pnl_overlap += [t["pnl"] for t in headline if t["session"] == s]
        fc_pnl_overlap += [f["pnl"] for f in fc[s]]
    result["fc_t13_same_session"] = {
        "overlap_sessions": len(overlap_sessions),
        "matched_direction_entries": len(improvements),
        "avg_price_improvement_pts": round(statistics.mean(improvements), 2)
        if improvements else None,
        "vpc_ev_on_overlap": round(statistics.mean(vpc_pnl_overlap), 2)
        if vpc_pnl_overlap else None,
        "fc_t13_ev_on_overlap": round(statistics.mean(fc_pnl_overlap), 2)
        if fc_pnl_overlap else None,
    }
    print(f"fc_t13 same-session: {result['fc_t13_same_session']}")

    # ---- controls ----
    print("\n=== controls ===")
    variant, stop_mode = headline_key.split("|")

    naive_sessions = {
        s: detect_setups(s, data[s][3], data[s][0], data[s][1], data[s][2],
                         q50_by_session.get(s), s in fomc, require_flow=False)
        for s in sessions
    }
    naive = panel(simulate_cell(naive_sessions, data, variant, stop_mode))
    result["control_naive"] = naive
    print(f"naive pullback (no flow condition): {naive}")

    closes = []
    for s in sessions:
        minutes, bars, *_ = data[s]
        idx = bisect_right(minutes, 959) - 1
        if idx >= 0:
            closes.append((s, bars[idx][3]))
    prev_close: dict[str, int] = {
        closes[j][0]: (1 if closes[j - 1][1] >= closes[j - 2][1] else -1)
        for j in range(2, len(closes))
    }
    trend_prev_sessions = {
        s: detect_setups(s, data[s][3], data[s][0], data[s][1], data[s][2],
                         q50_by_session.get(s), s in fomc,
                         trend_override=prev_close.get(s))
        for s in sessions if s in prev_close
    }
    tp = panel(simulate_cell(trend_prev_sessions, data, variant, stop_mode))
    result["control_trend_prev"] = tp
    print(f"trend from previous day's close: {tp}")

    observed = cells[headline_key].get("ev", 0.0)
    rng = random.Random(20260817)
    perm_evs = []
    for _ in range(args.permutations):
        permuted = {}
        for s in sessions:
            signs = {i: rng.choice((1, -1)) for i in range(5, 391, 5)}
            permuted[s] = detect_setups(
                s, data[s][3], data[s][0], data[s][1], data[s][2],
                q50_by_session.get(s), s in fomc, f1_signs=signs)
        trades = simulate_cell(permuted, data, variant, stop_mode)
        perm_evs.append(statistics.mean([t["pnl"] for t in trades]) if trades else 0.0)
    p_value = sum(ev >= observed for ev in perm_evs) / len(perm_evs)
    result["control_permutation"] = {
        "n_permutations": len(perm_evs),
        "observed_ev": observed,
        "perm_ev_mean": round(statistics.mean(perm_evs), 2),
        "p_value": round(p_value, 3),
    }
    print(f"permutation: {result['control_permutation']}")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "phase1.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (args.out / "headline_trades.json").write_text(
        json.dumps(headline, indent=2), encoding="utf-8"
    )
    print(f"\nwritten -> {args.out / 'phase1.json'}")


if __name__ == "__main__":
    main()
