"""ICT-1: two $0 Phase-1 studies of ICT concepts x order flow (FVG, sweep).

The batch takes the two most falsifiable ICT (Inner Circle Trader)
claims and runs them through the house machinery -- pattern-only on the
full 2020->present fixture span, then flow-confirmed on the tick year
where the decision files carry F1_5 / z_eff:

  ICT-1.1 FVG   Fair value gaps: the "gaps get filled" claim measured
                descriptively, then displacement-FVG retrace entries
                (limit at consequent encroachment = gap midpoint) in the
                displacement direction. Cells: all-RTH, Silver Bullet
                (first FVG completing 10:00-11:00 ET), flow-aligned
                displacement (|F1_5| >= Q70 with the gap, tick year).
  ICT-1.2 SWEEP Liquidity sweep / turtle soup: previous-RTH high/low
                breached intraday, then a 5m close back inside within 6
                bars -> fade at that close. Cells: all-RTH, reclaim
                initiative flow (|F1_5| >= Q70 in fade direction),
                absorption at the extreme (z_eff <= -1 while beyond the
                level). Mirror: no reclaim within 6 bars -> continuation
                entry. Companion: native target at previous-day mid.

Geometry (house, FMB-1): 1 NQ, fraction-rebased 100/50 bracket (0.3306%
/ 0.1653% at spec-lock NQ 30250), $10/RT, market entries next-1m-open
+ 1 tick adverse, LIMIT entries filled at touch (no adverse tick),
stop-markets 1 further tick adverse, adverse-first inside every 1m bar,
15:55 flatten. Detection on 5m bars aggregated from the 1m fixtures;
execution on 1m. One position at a time per study; FVG cap 2/session,
sweep cap 1/side/session. FOMC: no entries after 13:00 ET.

Pre-registered gates (locked before any result was computed):
  frequency  >= 8 qualifying events/month median per headline cell,
             else that study is SHELVED (VPC precedent) -- P&L recorded
             but not gateable.
  PASS       hit >= 42% AND n >= 50 AND EV t >= 2.0 AND day-shuffle
             permutation p < 0.05.  KILL line: hit < 36% (break-even
             ~ 34% with costs).  Between: not passed, direction noted.
  controls   day-shuffle permutation (entry (minute, direction) pairs
             replayed as market entries on 100 random other sessions --
             kills the condition, keeps time-of-day and drift); mirror
             (directions flipped) recorded for inversions.

Priors, recorded now (2026-08-17), before results:
  1. FVG same-session fill rate will be high and mechanical -- a fact
     about bar overlap, not an edge.
  2. FVG retrace continuation is continuation-family: genuinely open.
     If anything passes, it should be the flow-aligned cell -- F1_5 is
     the repo's only validated direction source (~$280/trade in TFR's
     permutation control).
  3. Sweep FADE is expected NEGATIVE: every fade family in this repo
     died (AFR, DDR, ODC inversion, naive VWAP fades). The absorption
     cell is the recorded collision: ICT reads absorbed buyers at a
     swept high as reversal fuel; AFR measured absorbed aggression
     preceding MORE of the absorbed direction. AFR's prior says even
     the flow-confirmed fade fails.
  4. Sweep CONTINUATION (the mirror) leans weakly positive by the same
     house evidence.
  5. The sweep-vs-breakout base rate (how often a breach reclaims
     within 30 min at all) is open -- ICT's premise, measured.

Usage:
  uv run python scripts/ict1_studies.py [fvg|sweep|all] [--out var/ict1]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FIX_1M = Path("var/fixtures/1m")
DECISIONS = Path("var/decisions/k3m")
FOMC = Path("config/fomc_dates.json")

POINT_VALUE = 20.0
RT_COST = 10.0
TICK = 0.25
P_REF = 30250.0
TGT_F = 100.0 / P_REF               # 0.3306%
STP_F = 50.0 / P_REF                # 0.1653%
FLATTEN_MIN = 954                   # close of this 1m bar = 15:55 flatten
FOMC_CUTOFF = 780                   # 13:00 ET
RTH_OPEN, RTH_CLOSE = 570, 959      # 09:30 .. 15:59 minutes-from-midnight

# FVG spec
FVG_DISP_MULT = 1.5                 # displacement bar range vs trailing median
FVG_TRAIL = 20                      # 5m bars in the trailing range median
FVG_MIN_GAP = 0.0002                # 0.02% of price: dust filter
FVG_FORM_FIRST = 585                # c-bar close 09:45 ..
FVG_FORM_LAST = 870                 # .. 14:30
FVG_ARM_LAST = 900                  # limit dies unfilled at 15:00
FVG_CAP = 2
SB_FIRST, SB_LAST = 600, 660        # silver bullet: c-close in 10:00-11:00

# sweep spec
SWEEP_RECLAIM_BARS = 6              # 5m closes allowed for the reclaim
SWEEP_FIRST = 575                   # breaches counted from 09:35
SWEEP_LAST = 900                    # entry close by 15:00


# ---------------------------------------------------------------- loaders


def load_bars(session: str) -> tuple[list[int], list[tuple]]:
    """Per-minute (o, h, l, c) from the 4-print pseudo-tick fixture."""
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


def five_min(minutes: list[int], bars: list[tuple]) -> tuple[list[int], list[tuple]]:
    """RTH 5m bars keyed by CLOSE minute (575 = the 09:30-09:35 bar)."""
    out_m: list[int] = []
    out_b: list[tuple] = []
    group: list[tuple] = []
    for m, bar in zip(minutes, bars):
        if not RTH_OPEN <= m <= RTH_CLOSE:
            continue
        group.append(bar)
        if (m + 1 - RTH_OPEN) % 5 == 0:
            if len(group) == 5:
                out_m.append(m + 1)
                out_b.append((
                    group[0][0],
                    max(b[1] for b in group),
                    min(b[2] for b in group),
                    group[-1][3],
                ))
            group = []
    return out_m, out_b


def fomc_dates() -> set[str]:
    return set(json.loads(FOMC.read_text())["dates"])


def flow_day(session: str) -> dict | None:
    path = DECISIONS / f"{session}.json"
    if not path.exists():
        return None
    day = json.loads(path.read_text())
    if day.get("model") is None or not day.get("q_f1"):
        return None
    return day


def flow_at(day: dict | None, close_minute: int) -> tuple[float | None, float | None, float | None]:
    """(f1_5, z_eff, q70) at the 5m decision bar closing at close_minute."""
    if day is None:
        return None, None, None
    rec = day.get("bars", {}).get(str(close_minute - 570))
    if not isinstance(rec, dict):
        return None, None, None
    q70 = float(day["q_f1"].get("70", "nan"))
    return rec.get("f1_5"), rec.get("z_eff"), q70


# ---------------------------------------------------------------- bracket


def bracket_walk(
    minutes: list[int], bars: list[tuple], start_idx: int, entry: float, direction: int
) -> dict:
    """House fraction bracket from a known entry fill, adverse-first."""
    tgt = entry * (1 + direction * TGT_F)
    stp = entry * (1 - direction * STP_F)
    exit_px, outcome, exit_min = bars[-1][3], "time", minutes[-1]
    for k in range(start_idx, len(minutes)):
        m = minutes[k]
        o, h, l, c = bars[k]
        if (l <= stp) if direction == 1 else (h >= stp):
            gapped = (o < stp) if direction == 1 else (o > stp)
            exit_px = o if gapped else stp - direction * TICK
            outcome, exit_min = "stop", m
            break
        if (h >= tgt) if direction == 1 else (l <= tgt):
            exit_px, outcome, exit_min = tgt, "target", m
            break
        if m >= FLATTEN_MIN:
            exit_px, outcome, exit_min = c, "time", m
            break
    pnl = direction * (exit_px - entry) * POINT_VALUE - RT_COST
    return {"outcome": outcome, "pnl": round(pnl, 2), "exit_minute": exit_min}


def market_entry(minutes: list[int], bars: list[tuple], minute: int, direction: int) -> dict | None:
    """Market at `minute`'s 1m open + 1 tick adverse, then the house bracket."""
    i = bisect_left(minutes, minute)
    if i >= len(minutes) or minutes[i] != minute:
        return None
    entry = bars[i][0] + direction * TICK
    return {"entry": entry, **bracket_walk(minutes, bars, i, entry, direction)}


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
    hits = sum(t["outcome"] == "target" for t in trades)
    stops = sum(t["outcome"] == "stop" for t in trades)
    monthly: dict[str, int] = defaultdict(int)
    for t in trades:
        monthly[t["session"][:7]] += 1
    counts = list(monthly.values())
    return {
        "n": n,
        "hit_rate": round(hits / n, 4),
        "stop_rate": round(stops / n, 4),
        "time_rate": round((n - hits - stops) / n, 4),
        "ev": round(statistics.mean(pnls), 2),
        "total": round(sum(pnls), 2),
        "t": round(t_stat(pnls), 2),
        "per_month_median": statistics.median(counts) if counts else 0,
    }


def shuffle_control(
    trades: list[dict], data: dict[str, tuple], n_perm: int, seed: int
) -> dict:
    """Entry (minute, direction) pairs replayed on random other sessions."""
    if not trades:
        return {"n_permutations": 0}
    rng = random.Random(seed)
    sessions = list(data.keys())
    observed = statistics.mean([t["pnl"] for t in trades])
    perm_evs = []
    for _ in range(n_perm):
        pnls = []
        for t in trades:
            s = rng.choice(sessions)
            minutes, bars = data[s][0], data[s][1]
            res = market_entry(minutes, bars, t["entry_minute"], t["dir"])
            if res is not None:
                pnls.append(res["pnl"])
        perm_evs.append(statistics.mean(pnls) if pnls else 0.0)
    return {
        "n_permutations": n_perm,
        "observed_ev": round(observed, 2),
        "perm_ev_mean": round(statistics.mean(perm_evs), 2),
        "perm_ev_sd": round(statistics.stdev(perm_evs), 2) if len(perm_evs) > 2 else None,
        "p_value": round(sum(ev >= observed for ev in perm_evs) / n_perm, 3),
    }


def mirror(trades: list[dict], data: dict[str, tuple]) -> dict:
    flipped = []
    for t in trades:
        minutes, bars = data[t["session"]][0], data[t["session"]][1]
        res = market_entry(minutes, bars, t["entry_minute"], -t["dir"])
        if res is not None:
            flipped.append({**res, "session": t["session"]})
    return panel(flipped)


# ---------------------------------------------------------------- ICT-1.1 FVG


def study_fvg(data: dict[str, tuple], fomc: set[str], n_perm: int) -> dict:
    all_gaps = 0
    filled_same_session = 0
    bars_to_fill: list[int] = []
    trades: list[dict] = []

    for session, (minutes, bars, m5, b5, day) in data.items():
        ranges = [b[1] - b[2] for b in b5]
        session_trades = 0
        busy_until = -1
        for j in range(2, len(m5)):
            if session_trades >= FVG_CAP:
                break
            a, b, c = b5[j - 2], b5[j - 1], b5[j]
            close_minute = m5[j]
            direction = 0
            if a[1] < c[2] and b[3] > b[0]:
                direction, gap_lo, gap_hi = 1, a[1], c[2]
            elif a[2] > c[1] and b[3] < b[0]:
                direction, gap_lo, gap_hi = -1, c[1], a[2]
            if direction == 0:
                continue
            price = c[3]
            if (gap_hi - gap_lo) < FVG_MIN_GAP * price:
                continue
            trail = ranges[max(0, j - 1 - FVG_TRAIL): j - 1]
            if len(trail) < 5 or (b[1] - b[2]) < FVG_DISP_MULT * statistics.median(trail):
                continue
            all_gaps += 1

            # descriptive: same-session full fill (price trades through the
            # far side of the gap after formation)
            far = gap_lo if direction == 1 else gap_hi
            i0 = bisect_left(minutes, close_minute)
            for k in range(i0, len(minutes)):
                if (bars[k][2] <= far) if direction == 1 else (bars[k][1] >= far):
                    filled_same_session += 1
                    bars_to_fill.append(minutes[k] - close_minute)
                    break

            # entry: limit at consequent encroachment, armed from c close
            if not (FVG_FORM_FIRST <= close_minute <= FVG_FORM_LAST):
                continue
            if session in fomc and close_minute > FOMC_CUTOFF:
                continue
            if close_minute < busy_until:
                continue
            ce = (gap_lo + gap_hi) / 2.0
            fill_idx = None
            for k in range(i0, len(minutes)):
                if minutes[k] > FVG_ARM_LAST:
                    break
                o, h, l, cl = bars[k]
                touched = (l <= ce) if direction == 1 else (h >= ce)
                # invalidation BEFORE fill: a 1m close through the far side
                dead = (cl < gap_lo) if direction == 1 else (cl > gap_hi)
                if touched:
                    fill_idx = k
                    break
                if dead:
                    break
            if fill_idx is None:
                continue
            entry = ce
            result = bracket_walk(minutes, bars, fill_idx, entry, direction)
            f1, z_eff, q70 = flow_at(day, m5[j - 1])   # displacement bar flow
            flow_aligned = (
                f1 is not None and q70 == q70 and f1 * direction > 0 and abs(f1) >= q70
            )
            trades.append({
                "session": session,
                "dir": direction,
                "entry": entry,
                "entry_minute": minutes[fill_idx],
                "signal_minute": close_minute,
                "silver_bullet": SB_FIRST <= close_minute <= SB_LAST,
                "flow_aligned": flow_aligned,
                "has_flow": f1 is not None,
                **result,
            })
            session_trades += 1
            busy_until = result["exit_minute"] + 1

    flow_trades = [t for t in trades if t["has_flow"]]
    result = {
        "descriptive": {
            "qualifying_gaps": all_gaps,
            "fill_rate_same_session": round(filled_same_session / all_gaps, 4) if all_gaps else None,
            "median_minutes_to_fill": statistics.median(bars_to_fill) if bars_to_fill else None,
        },
        "cells": {
            "all": panel(trades),
            "silver_bullet": panel([t for t in trades if t["silver_bullet"]]),
            "tick_year_all": panel(flow_trades),
            "tick_year_flow_aligned": panel([t for t in flow_trades if t["flow_aligned"]]),
            "tick_year_no_flow": panel([t for t in flow_trades if not t["flow_aligned"]]),
        },
    }
    return result, trades


# ---------------------------------------------------------------- ICT-1.2 sweep


def study_sweep(data: dict[str, tuple], fomc: set[str], n_perm: int) -> dict:
    fades: list[dict] = []
    continuations: list[dict] = []
    native: list[dict] = []
    breaches = 0
    reclaimed_within = 0
    depths: list[float] = []

    prev_rth: dict[str, tuple] = {}
    last = None
    for session in data:
        minutes, bars, *_ = data[session]
        lo = bisect_left(minutes, RTH_OPEN)
        hi = bisect_right(minutes, RTH_CLOSE)
        if hi > lo:
            hi_px = max(bars[k][1] for k in range(lo, hi))
            lo_px = min(bars[k][2] for k in range(lo, hi))
            if last is not None:
                prev_rth[session] = last
            last = (hi_px, lo_px)

    for session, (minutes, bars, m5, b5, day) in data.items():
        if session not in prev_rth:
            continue
        pdh, pdl = prev_rth[session]
        pd_mid = (pdh + pdl) / 2.0
        for side, level, direction in (("high", pdh, -1), ("low", pdl, 1)):
            # first 1m breach of the level
            breach_idx = None
            for k in range(bisect_left(minutes, SWEEP_FIRST), len(minutes)):
                if minutes[k] > SWEEP_LAST:
                    break
                if (bars[k][1] > level) if side == "high" else (bars[k][2] < level):
                    breach_idx = k
                    break
            if breach_idx is None:
                continue
            breaches += 1
            breach_minute = minutes[breach_idx]
            # depth of the sweep while it lasted (max penetration in window)
            j0 = bisect_left(m5, breach_minute)
            window = range(j0, min(j0 + SWEEP_RECLAIM_BARS, len(m5)))
            if side == "high":
                depth = max((b5[j][1] - level for j in window), default=0.0)
            else:
                depth = max((level - b5[j][2] for j in window), default=0.0)
            depths.append(depth / level)
            # reclaim: first 5m close back inside within the window
            reclaim_j = None
            absorbed = False
            for j in window:
                f1, z_eff, q70 = flow_at(day, m5[j])
                if z_eff is not None and z_eff <= -1.0:
                    absorbed = True
                inside = (b5[j][3] < level) if side == "high" else (b5[j][3] > level)
                if inside:
                    reclaim_j = j
                    break
            if reclaim_j is not None:
                reclaimed_within += 1
                close_minute = m5[reclaim_j]
                if close_minute > SWEEP_LAST:
                    continue
                if session in fomc and close_minute > FOMC_CUTOFF:
                    continue
                res = market_entry(minutes, bars, close_minute, direction)
                if res is None:
                    continue
                f1, z_eff, q70 = flow_at(day, close_minute)
                initiative = (
                    f1 is not None and q70 == q70 and f1 * direction > 0 and abs(f1) >= q70
                )
                fades.append({
                    "session": session, "side": side, "dir": direction,
                    "entry_minute": close_minute, "depth_frac": depth / level,
                    "initiative": initiative, "absorbed": absorbed,
                    "has_flow": f1 is not None, **res,
                })
                # native-target companion: limit at PD mid, house stop
                entry = res["entry"]
                tgt = pd_mid
                stp = entry * (1 - direction * STP_F)
                if direction * (tgt - entry) > TICK:
                    exit_px, outcome, exit_min = None, None, None
                    i0 = bisect_left(minutes, close_minute)
                    for k in range(i0, len(minutes)):
                        m = minutes[k]
                        o, h, l, c = bars[k]
                        if (l <= stp) if direction == 1 else (h >= stp):
                            gapped = (o < stp) if direction == 1 else (o > stp)
                            exit_px = o if gapped else stp - direction * TICK
                            outcome, exit_min = "stop", m
                            break
                        if (h >= tgt) if direction == 1 else (l <= tgt):
                            exit_px, outcome, exit_min = tgt, "target", m
                            break
                        if m >= FLATTEN_MIN:
                            exit_px, outcome, exit_min = c, "time", m
                            break
                    if exit_px is None:
                        exit_px, outcome = bars[-1][3], "time"
                    native.append({
                        "session": session,
                        "outcome": outcome,
                        "pnl": round(direction * (exit_px - entry) * POINT_VALUE - RT_COST, 2),
                    })
            else:
                # no reclaim: continuation entry at the first 5m close after
                # the window, still beyond the level
                j_next = j0 + SWEEP_RECLAIM_BARS
                if j_next >= len(m5):
                    continue
                close_minute = m5[j_next]
                still_out = (b5[j_next][3] > level) if side == "high" else (b5[j_next][3] < level)
                if not still_out or close_minute > SWEEP_LAST:
                    continue
                if session in fomc and close_minute > FOMC_CUTOFF:
                    continue
                res = market_entry(minutes, bars, close_minute, -direction)
                if res is not None:
                    continuations.append({
                        "session": session, "side": side, "dir": -direction,
                        "entry_minute": close_minute, **res,
                    })

    flow_fades = [t for t in fades if t["has_flow"]]
    result = {
        "descriptive": {
            "breaches": breaches,
            "reclaim_rate_within_30m": round(reclaimed_within / breaches, 4) if breaches else None,
            "median_sweep_depth_frac": round(statistics.median(depths), 6) if depths else None,
        },
        "cells": {
            "fade_all": panel(fades),
            "fade_tick_year": panel(flow_fades),
            "fade_initiative_flow": panel([t for t in flow_fades if t["initiative"]]),
            "fade_absorbed_extreme": panel([t for t in flow_fades if t["absorbed"]]),
            "continuation_no_reclaim": panel(continuations),
            "native_target_pd_mid": panel(native),
        },
    }
    return result, fades, continuations


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", nargs="?", default="all", choices=("fvg", "sweep", "all"))
    parser.add_argument("--out", type=Path, default=Path("var/ict1"))
    parser.add_argument("--permutations", type=int, default=100)
    args = parser.parse_args()

    fomc = fomc_dates()
    sessions = sorted(p.stem for p in FIX_1M.glob("*.jsonl"))
    print(f"{len(sessions)} sessions {sessions[0]}..{sessions[-1]}")
    data: dict[str, tuple] = {}
    for s in sessions:
        minutes, bars = load_bars(s)
        if not minutes:
            continue
        m5, b5 = five_min(minutes, bars)
        if len(m5) < 30:
            continue
        data[s] = (minutes, bars, m5, b5, flow_day(s))

    args.out.mkdir(parents=True, exist_ok=True)
    out: dict = {}

    if args.study in ("fvg", "all"):
        print("\n=== ICT-1.1 FVG ===")
        fvg, fvg_trades = study_fvg(data, fomc, args.permutations)
        fvg["controls"] = {
            "day_shuffle": shuffle_control(fvg_trades, data, args.permutations, 20260817),
            "mirror": mirror(fvg_trades, data),
        }
        for k, v in fvg["descriptive"].items():
            print(f"  {k}: {v}")
        for k, v in fvg["cells"].items():
            print(f"  cell {k}: {v}")
        print(f"  controls: {fvg['controls']}")
        out["fvg"] = fvg
        (args.out / "fvg.json").write_text(json.dumps(fvg, indent=2), encoding="utf-8")

    if args.study in ("sweep", "all"):
        print("\n=== ICT-1.2 SWEEP ===")
        sweep, fades, conts = study_sweep(data, fomc, args.permutations)
        sweep["controls"] = {
            "day_shuffle_fade": shuffle_control(fades, data, args.permutations, 20260818),
            "mirror_fade": mirror(fades, data),
        }
        for k, v in sweep["descriptive"].items():
            print(f"  {k}: {v}")
        for k, v in sweep["cells"].items():
            print(f"  cell {k}: {v}")
        print(f"  controls: {sweep['controls']}")
        out["sweep"] = sweep
        (args.out / "sweep.json").write_text(json.dumps(sweep, indent=2), encoding="utf-8")

    print(f"\nwritten -> {args.out}")


if __name__ == "__main__":
    main()
