"""MAE-1: minimum viable stop study on fc_t13 flow entries, $0 descriptive.

Spec MAE-1.md v1.0.  Column-join on the fc_t13 develop journal + shadow
trades within fixture coverage: from each recorded fill, walk 1m bars to
the actual exit minute, record MAE/MFE, then re-simulate a stop-at-S
ladder to find the smallest stop that keeps the edge net-positive --
unconditionally and within five pre-declared entry-time subsets.

Definitions implemented:

  fill       journal entry_price (decision-bar close +- 1 tick adverse,
             verified against decisions bars); path = 1m bars from the
             entry minute (inclusive) to the exit minute (exclusive) --
             the exit fill prints at the exit minute's open.
  MAE / MFE  max adverse / favorable excursion vs fill, points (MAE also
             as % of fill).  Adverse-first within-bar where 1m OHLC is
             ambiguous (house convention): on the bar where the final
             MAE depth prints, adverse precedes any favorable print of
             that bar, so a same-bar tie counts MAE first.
  stop-at-S  trade stopped iff MAE >= S (touch); stop fill S + 1 tick
             adverse -> pnl = -(S + 0.25) * $20 * qty - $10 * qty.
             Unstopped trades keep their journal P&L.  The re-simulation
             reprices existing entries only; it creates no frequency.
  outcome    win: journal pnl > 0; scratch: pnl == -$10 (zero-point
             exit, cost only), counted inside losers for the ladder and
             journaled separately.

Ladder S in {10,15,20,25,30,35,40,50,60,85} pts (85 = catastrophic).
Minimum viable stop, pre-defined: smallest S with re-sim EV >= +$100 /
trade (NQ-equiv) AND PF >= 1.2.  If nothing below catastrophic clears
it, that is the answer.

Conditional cuts (knowable at entry, one pass):
  1. flow strength   |f1_5| percentile at the entry bar vs a trailing
                     60-session pool of all RTH-bar |f1_5| (>= 20
                     sessions, walk-forward).  Deviation disclosed: the
                     day q_f1 table tops out at Q85, so the trailing
                     pool defines the Q70-80 / 80-90 / 90+ bins;
                     sub-Q70 and unknown are residual bins.
  2. entry hour ET   09:35-10:30 / 10:30-12:00 / 12:00-15:00
  3. gamma regime    NEG / POS from var/gamma/regimes.jsonl by date
  4. vol_z tercile   z_vol at the entry bar, terciles over the study's
                     covered trades (in-sample binning, disclosed)
  5. immediate momentum  first 1m bar after fill closes favorable --
                     descriptive only (knowable one minute AFTER entry),
                     excluded from promotion, flagged.

Subset promotion bar (pre-registered): some S <= 30 with re-sim EV >=
+$150/trade, PF >= 1.3, winners-preserved >= 70%, n >= 60, median >= 6
entries/month over the journal span.  Looks = (1 + bins) x stops,
counted and printed.  A pass is a design license, not evidence -- the
develop year is exhausted and disclosed; any sleeve validates forward.

Usage:
  uv run python scripts/mae_study.py \
      --journals var/tfr/fc_t13/journal var/shadow/journal \
      --fixtures var/fixtures/1m --regimes var/gamma/regimes.jsonl \
      --decisions var/decisions/k3m --out var/mae1
"""

from __future__ import annotations

import argparse
import json
import statistics
from bisect import bisect_left
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

POINT_VALUE = 20.0
RT_COST = 10.0
TICK = 0.25
STOPS = (10, 15, 20, 25, 30, 35, 40, 50, 60, 85)
CATASTROPHIC = 85
FAV_MARK = 10.0                      # "first +10 pts favorable" marker
MVS_EV, MVS_PF = 100.0, 1.2          # minimum-viable-stop bar
PROMO_EV, PROMO_PF = 150.0, 1.3      # subset promotion bar
PROMO_WPRES, PROMO_N = 0.70, 60
PROMO_FREQ, PROMO_S_MAX = 6.0, 30
POOL_SESS, POOL_MIN_SESS = 60, 20    # trailing |f1_5| percentile pool
HOUR_BINS = ((575, 630, "09:35-10:30"), (630, 720, "10:30-12:00"),
             (720, 900, "12:00-15:00"))


# ---------------------------------------------------------------- loaders


def load_bars(path: Path) -> tuple[list[int], list[tuple]]:
    """Per-minute (o, h, l, c) from the 4-print pseudo-tick fixture."""
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
    cur_min = -1
    prices: list[float] = []
    for line in lines:
        parts = line.split('"')
        ts, price = parts[3], parts[7]
        minute = (int(ts[11:13]) * 60 + int(ts[14:16]) + offset) % 1440
        if minute != cur_min:
            if len(prices) == 4:
                minutes.append(cur_min)
                bars.append(tuple(prices))
            cur_min = minute
            prices = []
        prices.append(float(price))
    if len(prices) == 4:
        minutes.append(cur_min)
        bars.append(tuple(prices))
    return minutes, bars


def load_trades(journals: list[Path], fixtures: Path) -> tuple[list[dict], dict]:
    trades: list[dict] = []
    counts = {"develop": 0, "shadow": 0, "no_coverage": 0}
    for jdir in journals:
        source = "shadow" if "shadow" in str(jdir) else "develop"
        for path in sorted(jdir.glob("*.jsonl")):
            for line in path.read_text().splitlines():
                if '"position_closed"' not in line:
                    continue
                r = json.loads(line)
                if r.get("event") != "position_closed":
                    continue
                et_in = datetime.fromisoformat(r["entry_time"]).astimezone(ET)
                et_out = datetime.fromisoformat(r["exit_time"]).astimezone(ET)
                session = et_in.date().isoformat()
                pnl = float(r["realised_pnl"])
                if source == "shadow":                      # MNQ -> NQ-equiv
                    pnl *= 2.5 if session >= "2026-08-20" else 10.0
                if not (fixtures / f"{session}.jsonl").exists():
                    counts["no_coverage"] += 1
                    continue
                counts[source] += 1
                trades.append({
                    "source": source, "session": session,
                    "m_in": et_in.hour * 60 + et_in.minute,
                    "m_out": et_out.hour * 60 + et_out.minute,
                    "dir": 1 if r["direction"] == "LONG" else -1,
                    "qty": int(r.get("quantity", 1)),
                    "fill": float(r["entry_price"]),
                    "exit_price": float(r["exit_price"]),
                    "exit_reason": r.get("exit_reason"),
                    "pnl": pnl,
                })
    return trades, counts


def load_regimes(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["date"]] = r["regime"]
    return out


# ---------------------------------------------------------------- excursions


def walk_excursions(t: dict, minutes: list[int], bars: list[tuple]) -> bool:
    """MAE/MFE from fill to the exit minute (exclusive), adverse-first."""
    i0 = bisect_left(minutes, t["m_in"])
    i1 = bisect_left(minutes, t["m_out"])
    if i1 <= i0:
        return False
    d, fill = t["dir"], t["fill"]
    path = []
    for i in range(i0, i1):
        _, h, l, c = bars[i]
        adv = fill - l if d == 1 else h - fill
        fav = h - fill if d == 1 else fill - l
        path.append((minutes[i], max(adv, 0.0), max(fav, 0.0), c))
    mae = mfe = 0.0
    t_mae = None
    for m, adv, fav, _ in path:
        if adv > mae:
            mae, t_mae = adv, m
        mfe = max(mfe, fav)
    t_fav = next((m for m, _, fav, _ in path if fav >= FAV_MARK), None)
    mae_first = None
    if mae > 0:
        mae_first = True if t_fav is None else t_mae <= t_fav   # adverse-first tie
    c1 = path[0][3]
    t.update({
        "mae": round(mae, 2), "mfe": round(mfe, 2),
        "mae_pct": round(mae / fill * 100, 4),
        "time_to_mae_min": None if t_mae is None else t_mae - t["m_in"],
        "mae_before_fav10": mae_first,
        "bars_walked": len(path),
        "imm_mom": (c1 - fill) * d > 0,
    })
    return True


# ---------------------------------------------------------------- ladder


def resim_panel(rows: list[dict], s: float, sessions_order: list[str]) -> dict:
    """Stop-at-S re-simulation of one trade set."""
    winners = [t for t in rows if t["pnl"] > 0]
    losers = [t for t in rows if t["pnl"] <= 0]
    pnls = []
    per_day: dict[str, float] = {}
    stopped = 0
    for t in rows:
        if t["mae"] >= s:
            p = -((s + TICK) * POINT_VALUE + RT_COST) * t["qty"]
            stopped += 1
        else:
            p = t["pnl"]
        pnls.append(p)
        per_day[t["session"]] = per_day.get(t["session"], 0.0) + p
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    days = [d for d in sessions_order if d in per_day]
    worst5 = (min(sum(per_day[d] for d in days[i:i + 5])
                  for i in range(len(days) - 4)) if len(days) >= 5
              else sum(per_day[d] for d in days))
    ev = statistics.mean(pnls) if pnls else 0.0
    risk = (s + TICK) * POINT_VALUE
    return {
        "n": len(rows), "stopped": stopped,
        "winners_preserved": (round(sum(1 for t in winners if t["mae"] < s)
                                    / len(winners), 4) if winners else None),
        "losers_under_stop": (round(sum(1 for t in losers if t["mae"] < s)
                                    / len(losers), 4) if losers else None),
        "ev_usd": round(ev, 2),
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else None,
        "pf": round(gw / gl, 3) if gl else None,
        "avg_win": round(statistics.mean([p for p in pnls if p > 0]), 2)
                   if any(p > 0 for p in pnls) else None,
        "avg_loss": round(statistics.mean([p for p in pnls if p < 0]), 2)
                    if any(p < 0 for p in pnls) else None,
        "worst_5day_usd": round(worst5, 2),
        "ev_per_dollar_risked": round(ev / risk, 4),
    }


def ladder(rows: list[dict], sessions_order: list[str]) -> dict:
    out = {f"S{s}": resim_panel(rows, s, sessions_order) for s in STOPS}
    mvs = next((s for s in STOPS
                if out[f"S{s}"]["ev_usd"] >= MVS_EV
                and (out[f"S{s}"]["pf"] or 0) >= MVS_PF), None)
    out["min_viable_stop"] = mvs
    return out


def monthly_median(months: list[str], stamps: list[str]) -> float:
    per = {m: 0 for m in months}
    for s in stamps:
        per[s[:7]] += 1
    return statistics.median(per.values()) if per else 0.0


def dist(vals: list[float]) -> dict | None:
    if not vals:
        return None
    qs = statistics.quantiles(vals, n=10) if len(vals) >= 10 else None
    return {
        "n": len(vals), "mean": round(statistics.mean(vals), 2),
        "p10": round(qs[0], 2) if qs else None,
        "p25": round(statistics.quantiles(vals, n=4)[0], 2) if len(vals) >= 4 else None,
        "p50": round(statistics.median(vals), 2),
        "p75": round(statistics.quantiles(vals, n=4)[2], 2) if len(vals) >= 4 else None,
        "p90": round(qs[8], 2) if qs else None,
        "max": round(max(vals), 2),
    }


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journals", type=Path, nargs="+",
                        default=[Path("var/tfr/fc_t13/journal"),
                                 Path("var/shadow/journal")])
    parser.add_argument("--fixtures", type=Path, default=Path("var/fixtures/1m"))
    parser.add_argument("--regimes", type=Path, default=Path("var/gamma/regimes.jsonl"))
    parser.add_argument("--decisions", type=Path, default=Path("var/decisions/k3m"))
    parser.add_argument("--out", type=Path, default=Path("var/mae1"))
    args = parser.parse_args()

    trades, counts = load_trades(args.journals, args.fixtures)
    regimes = load_regimes(args.regimes)

    walked: list[dict] = []
    counts["no_bars"] = 0
    bar_cache: dict[str, tuple] = {}
    for t in trades:
        if t["session"] not in bar_cache:
            bar_cache[t["session"]] = load_bars(args.fixtures / f"{t['session']}.jsonl")
        if walk_excursions(t, *bar_cache[t["session"]]):
            walked.append(t)
        else:
            counts["no_bars"] += 1

    # entry-bar features + trailing |f1_5| percentile pool (walk-forward)
    all_sessions = sorted(p.stem for p in args.decisions.glob("*.json"))
    abs_f1: dict[str, list[float]] = {}
    day_bars: dict[str, dict] = {}
    for s in all_sessions:
        day = json.loads((args.decisions / f"{s}.json").read_text())
        day_bars[s] = day.get("bars", {})
        abs_f1[s] = [abs(b["f1_5"]) for b in day_bars[s].values()
                     if isinstance(b, dict) and b.get("f1_5") is not None]
    pool_cache: dict[str, list[float] | None] = {}
    for s in sorted({t["session"] for t in walked}):
        pool: list[float] = []
        got = 0
        i = bisect_left(all_sessions, s)
        for k in range(i - 1, -1, -1):
            if abs_f1.get(all_sessions[k]):
                pool.extend(abs_f1[all_sessions[k]])
                got += 1
                if got >= POOL_SESS:
                    break
        pool_cache[s] = sorted(pool) if got >= POOL_MIN_SESS else None

    for t in walked:
        idx = t["m_in"] - 570                      # ET minutes since 09:30
        rec = day_bars.get(t["session"], {}).get(str(idx))
        rec = rec if isinstance(rec, dict) else {}
        t["f1_5"] = rec.get("f1_5")
        t["z_vol"] = rec.get("z_vol")
        pool = pool_cache.get(t["session"])
        t["f1_pctile"] = (round(100 * bisect_left(pool, abs(t["f1_5"])) / len(pool), 2)
                          if pool and t["f1_5"] is not None else None)
        t["regime"] = regimes.get(t["session"])

    # ---- cuts (bin label per trade, knowable at entry except imm_mom)
    zv = sorted(t["z_vol"] for t in walked if t["z_vol"] is not None)
    z_terc = (statistics.quantiles(zv, n=3) if len(zv) >= 3 else None)

    def flow_bin(t: dict) -> str:
        p = t["f1_pctile"]
        if p is None:
            return "unknown"
        if p >= 90:
            return "Q90+"
        if p >= 80:
            return "Q80-90"
        if p >= 70:
            return "Q70-80"
        return "sub-Q70"

    def hour_bin(t: dict) -> str:
        m = t["m_in"]
        for i, (lo, hi, lbl) in enumerate(HOUR_BINS):
            if (lo <= m if i == 0 else lo < m) and m <= hi:
                return lbl
        return "other"

    def vol_bin(t: dict) -> str:
        if t["z_vol"] is None or z_terc is None:
            return "unknown"
        if t["z_vol"] < z_terc[0]:
            return "low"
        return "mid" if t["z_vol"] < z_terc[1] else "high"

    cuts = {
        "flow_strength": flow_bin,
        "entry_hour": hour_bin,
        "gamma_regime": lambda t: t["regime"] or "unknown",
        "vol_z_tercile": vol_bin,
        "imm_momentum": lambda t: "worked_bar1" if t["imm_mom"] else "adverse_bar1",
    }
    for t in walked:
        t["bins"] = {c: fn(t) for c, fn in cuts.items()}

    sessions_order = sorted({t["session"] for t in walked})
    months = sorted({s[:7] for s in sessions_order})
    span = [f"{y}-{m:02d}" for y in range(int(months[0][:4]), int(months[-1][:4]) + 1)
            for m in range(1, 13)]
    span = [m for m in span if months[0] <= m <= months[-1]]

    winners = [t for t in walked if t["pnl"] > 0]
    losers = [t for t in walked if t["pnl"] <= 0]

    out: dict = {"meta": {
        "run_date": "2026-08-21", "spec": "MAE-1 v1.0",
        "journal_counts": counts,
        "n_walked": len(walked), "n_winners": len(winners), "n_losers": len(losers),
        "n_scratch_cost_only": sum(1 for t in walked if t["pnl"] == -RT_COST),
        "sessions": [sessions_order[0], sessions_order[-1], len(sessions_order)],
        "params": {"stops": list(STOPS), "catastrophic": CATASTROPHIC,
                   "mvs_bar": [MVS_EV, MVS_PF],
                   "promotion_bar": {"ev": PROMO_EV, "pf": PROMO_PF,
                                     "winners_preserved": PROMO_WPRES,
                                     "n": PROMO_N, "per_month": PROMO_FREQ,
                                     "s_max": PROMO_S_MAX},
                   "f1_pool": [POOL_SESS, POOL_MIN_SESS]},
        "disclosures": [
            "flow percentiles from trailing-60-session |f1_5| pool (day q_f1 "
            "table tops out at Q85); sub-Q70/unknown are residual bins",
            "vol_z terciles binned over the study's own covered trades (in-sample)",
            "imm_momentum is knowable one minute AFTER entry: descriptive only, "
            "excluded from promotion",
            "shadow trades postdate 1m fixture coverage (> 2026-08-12): "
            f"{counts['no_coverage']} journaled uncovered, study rests on the "
            "develop journal",
            "develop year exhausted and contaminated; a passing subset is a "
            "design license, not evidence",
        ],
    }}

    # distributions
    out["distributions"] = {
        "mae_pts": {"winners": dist([t["mae"] for t in winners]),
                    "losers": dist([t["mae"] for t in losers])},
        "mae_pct_of_price": {"winners": dist([t["mae_pct"] for t in winners]),
                             "losers": dist([t["mae_pct"] for t in losers])},
        "mfe_pts": {"winners": dist([t["mfe"] for t in winners]),
                    "losers": dist([t["mfe"] for t in losers])},
        "time_to_mae_min": {"winners": dist([float(t["time_to_mae_min"]) for t in winners
                                             if t["time_to_mae_min"] is not None]),
                            "losers": dist([float(t["time_to_mae_min"]) for t in losers
                                            if t["time_to_mae_min"] is not None])},
        "mae_before_fav10_share": {
            "winners": (round(sum(1 for t in winners if t["mae_before_fav10"] is True)
                              / len(winners), 4) if winners else None),
            "losers": (round(sum(1 for t in losers if t["mae_before_fav10"] is True)
                             / len(losers), 4) if losers else None)},
    }

    # unconditional ladder
    out["unconditional"] = ladder(walked, sessions_order)

    # conditional ladders + promotion scan
    looks = len(STOPS)
    promoted: list[dict] = []
    out["cuts"] = {}
    for cut in cuts:
        out["cuts"][cut] = {}
        for lbl in sorted({t["bins"][cut] for t in walked}):
            rows = [t for t in walked if t["bins"][cut] == lbl]
            lad = ladder(rows, sessions_order)
            lad["per_month_median"] = monthly_median(
                span, [t["session"] for t in rows])
            looks += len(STOPS)
            passing = []
            if cut != "imm_momentum" and lbl != "unknown":
                for s in STOPS:
                    if s > PROMO_S_MAX:
                        continue
                    p = lad[f"S{s}"]
                    if (p["ev_usd"] >= PROMO_EV and (p["pf"] or 0) >= PROMO_PF
                            and (p["winners_preserved"] or 0) >= PROMO_WPRES
                            and p["n"] >= PROMO_N
                            and lad["per_month_median"] >= PROMO_FREQ):
                        passing.append(s)
            lad["promotion_stops"] = passing
            if passing:
                promoted.append({"cut": cut, "bin": lbl, "stops": passing,
                                 "n": len(rows)})
            out["cuts"][cut][lbl] = lad

    out["looks_disclosure"] = {
        "ev_evaluations": looks,
        "note": "1 unconditional + every (cut, bin) ladder x 10 stops; "
                "all looks printed, none hidden",
    }
    out["promoted_subsets"] = promoted

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "mae1.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    with (args.out / "trades.jsonl").open("w", encoding="utf-8") as fh:
        for t in walked:
            fh.write(json.dumps(t) + "\n")

    # ---- console summary
    print(json.dumps(out["meta"], indent=1))
    print("\n===== unconditional ladder =====")
    for s in STOPS:
        p = out["unconditional"][f"S{s}"]
        print(f"S{s:>3}: ev ${p['ev_usd']:>8.2f}  pf {p['pf'] or 0:>6.3f}  "
              f"wr {p['win_rate']:.3f}  wpres {p['winners_preserved']:.3f}  "
              f"stopped {p['stopped']:>3}  worst5 ${p['worst_5day_usd']:>9.2f}  "
              f"ev/$risk {p['ev_per_dollar_risked']:>7.4f}")
    print(f"min_viable_stop: {out['unconditional']['min_viable_stop']}")
    print("\n===== distributions =====")
    print(json.dumps(out["distributions"], indent=1))
    print("\n===== conditional min-viable-stops =====")
    for cut, bins in out["cuts"].items():
        for lbl, lad in bins.items():
            print(f"{cut:>15} / {lbl:<12} n={lad['S10']['n']:>3}  "
                  f"mvs={lad['min_viable_stop']}  "
                  f"per_month={lad['per_month_median']}  "
                  f"promo_stops={lad['promotion_stops']}")
    print(f"\nlooks: {looks} EV evaluations")
    print(f"promoted subsets: {json.dumps(promoted)}")
    print(f"\nwritten -> {args.out}")


if __name__ == "__main__":
    main()
