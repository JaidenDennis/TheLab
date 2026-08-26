"""SFB Phase 1: HTF liquidity sweeps x order-flow verdict, $0 on existing data.

Program 11 (spec SFB.md v1.0). High-timeframe liquidity levels as the
location, F1_5 in the K = 3 5m bars after the breach as the verdict,
two branches:

  REVERSAL      the first 5m close back inside the level, and THAT bar's
                F1_5 initiative against the breach (|F1_5| >= day Q70,
                sign opposing).  A reclaim without counter-initiative on
                the reclaim bar itself is NEITHER -- later flow does not
                resurrect it.  This is the strict, continuous reading of
                ICT-1.2's one live cell (+$261/trade, n = 23).
  CONTINUATION  no 5m close back inside within K bars, and F1_5
                initiative WITH the breach on >= 2 of the K bars.
                Verdict resolves at bar K's close.
  NEITHER       everything else.  No trade, measured as the control
                bucket (expected majority; must be ~flat or the verdict
                is not doing the work).

Level catalog (walk-forward, swept at most once per level per session,
detection 09:35-14:30 on 1m, breach = trade >= B = 4 ticks beyond after
>= 30 consecutive 1m bars with no trade beyond the level):

  PD   prior RTH session high/low (the ICT-1.2 known-hostile benchmark)
  PW   prior completed week's high/low (all fixture minutes of that
       ISO week; Sunday-evening Globex is absent from the calendar-day
       fixtures -- journaled limitation)
  MT   completed Globex-aligned 4H candle extreme (boundaries 18:00/
       22:00/02:00/06:00/10:00/14:00 ET, candle kept if >= 120 minutes
       present) touched >= 2 further times within T_tol = 10 ticks
       (touch: extreme within tol, no close beyond) without a 4H close
       beyond, earliest formation within the last 15 sessions,
       liveness/touches evaluated at breach time (intraday 4H closes
       at 10:00/14:00 count)
  ON   overnight high/low: prior file 18:00-24:00 + session file
       00:00-09:30 (Monday misses Sunday 18:00-24:00 -- journaled)

Confluence: two levels of different classes breached on the same 1m bar
with prices within 10 ticks -> one event, tagged the highest class
(PW > MT > PD > ON), confluence flagged.

Phase 1 is bracket-free.  Forward returns on RTH 5m closes at horizons
1/3/6/13 bars; branch EV$ modeled as (fwd_pts - 1 tick adverse) * $20
- $10 RT.  FOMC sessions: events with verdict close after 13:00 ET are
excluded from branch/frequency cells (journaled), kept in descriptive
hierarchy panels.

Pre-registered gates (spec section 4, locked before any result):
  frequency  combined REVERSAL+CONTINUATION events >= 8/month median
             (zero-months included), else SHELVED -- never loosened.
  go/no-go   each branch fwd return positive with T >= 1.5 at >= 2
             horizons, n >= 50 per branch, AND NEITHER ~ flat.
  controls   naked replication per class must lose (a positive naked
             class is a finding, recorded not traded); day-shuffle
             placement; F1_5 sign-scramble must destroy both branches.

Priors, recorded before results (2026-08-20, from the spec):
  1. Reversal fights seven dead fade families; only the flow condition
     can rescue it.  Continuation fights ICT-1.2's -$74 continuation;
     only flow persistence can rescue it.
  2. If the verdict does not separate the branches, SFB dies as the
     eighth and ninth fade/chase and the report says so.
  3. The hierarchy question (weekly vs 4H-MT vs daily) is genuinely
     open -- untested anywhere in the repo.
  4. NEITHER is expected to be the majority verdict.

Usage:
  uv run python scripts/sfb_study.py [--out var/sfb] [--permutations 200]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FIX = Path("var/fixtures/1m-full")
DECISIONS = Path("var/decisions/k3m")
FOMC = Path("config/fomc_dates.json")

POINT_VALUE = 20.0
RT_COST = 10.0
TICK = 0.25
B_PTS = 4 * TICK                    # breach threshold: 4 ticks
T_TOL = 10 * TICK                   # 4H-MT touch tolerance: 10 ticks
K = 3                               # verdict window, 5m bars
INSIDE_MIN = 30                     # 1m bars inside before a breach counts
SWEEP_FIRST, SWEEP_LAST = 575, 870  # breach 1m bar in 09:35..14:30
SCAN_FROM = 480                     # streak seeding starts 08:00
RTH_OPEN, RTH_CLOSE = 570, 959
FOMC_CUTOFF = 780                   # 13:00 ET
HORIZONS = (1, 3, 6, 13)
MT_MAX_AGE = 15                     # sessions
MT_MIN_TOUCH = 2
CANDLE_MIN_MINUTES = 120
CLASS_RANK = {"PW": 4, "MT": 3, "PD": 2, "ON": 1}
GATE_T = 1.5
GATE_N = 50
GATE_FREQ = 8.0


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


def rth_5m(minutes: list[int], bars: list[tuple]) -> tuple[list[int], list[tuple]]:
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


def f1_at(day: dict | None, close_minute: int) -> tuple[float | None, float | None]:
    """(f1_5, day q70) at the 5m bar closing at close_minute."""
    if day is None:
        return None, None
    rec = day.get("bars", {}).get(str(close_minute - 570))
    if not isinstance(rec, dict) or rec.get("f1_5") is None:
        return None, None
    q70 = float(day["q_f1"].get("70", "nan"))
    if q70 != q70:
        return None, None
    return rec["f1_5"], q70


# ---------------------------------------------------------------- catalog


def build_candles(sessions: list[str], data: dict[str, tuple]) -> list[dict]:
    """Globex-aligned 4H candles from the chronological minute timeline.

    Bucket key handles the 22:00-02:00 candle spanning midnight; a
    candle is kept only with >= CANDLE_MIN_MINUTES bars (weekend and
    halt truncations journaled by the count field).
    """
    def bucket_key(d: str, m: int) -> tuple[str, int]:
        if m >= 1320:
            return (d, 1320)
        if m < 120:
            prev = (date_cls.fromisoformat(d) - timedelta(days=1)).isoformat()
            return (prev, 1320)
        start = 120 + 240 * ((m - 120) // 240)
        return (d, start)

    candles: list[dict] = []
    cur_key = None
    cur: dict | None = None
    for d in sessions:
        minutes, bars = data[d][0], data[d][1]
        for m, (o, h, l, c) in zip(minutes, bars):
            key = bucket_key(d, m)
            if key != cur_key:
                if cur is not None and cur["n"] >= CANDLE_MIN_MINUTES:
                    candles.append(cur)
                cur_key = key
                cur = {"high": h, "low": l, "close": c,
                       "end_date": d, "end_minute": m, "n": 1}
            else:
                cur["high"] = max(cur["high"], h)
                cur["low"] = min(cur["low"], l)
                cur["close"] = c
                cur["end_date"] = d
                cur["end_minute"] = m
                cur["n"] += 1
    if cur is not None and cur["n"] >= CANDLE_MIN_MINUTES:
        candles.append(cur)
    return candles


def mt_status(cand: dict, candles: list[dict], upto: tuple[str, int]) -> tuple[int, bool]:
    """(touch count, dead) for a 4H extreme, over candles completed before `upto`."""
    side, price = cand["side"], cand["price"]
    touches, dead = 0, False
    for c in candles[cand["idx"] + 1:]:
        if (c["end_date"], c["end_minute"]) >= upto:
            break
        if side == "high":
            if c["close"] > price:
                dead = True
                break
            if c["high"] >= price - T_TOL:
                touches += 1
        else:
            if c["close"] < price:
                dead = True
                break
            if c["low"] <= price + T_TOL:
                touches += 1
    return touches, dead


def weekly_extremes(sessions: list[str], data: dict[str, tuple]) -> dict[str, tuple]:
    """Prior completed ISO week's (high, low) per session date."""
    by_week: dict[tuple, list[str]] = defaultdict(list)
    for d in sessions:
        by_week[date_cls.fromisoformat(d).isocalendar()[:2]].append(d)
    weeks = sorted(by_week)
    ext: dict[tuple, tuple] = {}
    for w in weeks:
        hi = max(max(b[1] for b in data[d][1]) for d in by_week[w])
        lo = min(min(b[2] for b in data[d][1]) for d in by_week[w])
        ext[w] = (hi, lo)
    out: dict[str, tuple] = {}
    for i, w in enumerate(weeks):
        if i == 0:
            continue
        for d in by_week[w]:
            out[d] = ext[weeks[i - 1]]
    return out


# ---------------------------------------------------------------- sweeps


def scan_level(minutes: list[int], bars: list[tuple], side: str, price: float) -> int | None:
    """First 1m bar >= B_PTS beyond `price` after >= INSIDE_MIN bars with no
    trade beyond, breach minute in [SWEEP_FIRST, SWEEP_LAST]. None if no event."""
    i0 = bisect_left(minutes, SCAN_FROM)
    streak = 0
    for k in range(i0, len(minutes)):
        m = minutes[k]
        if m > SWEEP_LAST:
            return None
        h, l = bars[k][1], bars[k][2]
        beyond = h > price if side == "high" else l < price
        breach = h >= price + B_PTS if side == "high" else l <= price - B_PTS
        if breach and streak >= INSIDE_MIN and m >= SWEEP_FIRST:
            return m
        streak = 0 if beyond else streak + 1
    return None


def find_events(sessions: list[str], data: dict[str, tuple],
                candles: list[dict], pw: dict[str, tuple]) -> tuple[list[dict], dict]:
    """Sweep events across all classes, confluence-merged, verdicts attached."""
    counts = defaultdict(int)
    events: list[dict] = []

    # 4H extreme candidates: dedup (side, price) keeping earliest formation
    cand_all: list[dict] = []
    for i, c in enumerate(candles):
        for side, price in (("high", c["high"]), ("low", c["low"])):
            cand_all.append({"side": side, "price": price, "idx": i,
                             "end_date": c["end_date"], "end_minute": c["end_minute"]})
    earliest: dict[tuple, dict] = {}
    for cand in cand_all:
        key = (cand["side"], round(cand["price"], 2))
        if key not in earliest:
            earliest[key] = cand
    sess_idx = {d: i for i, d in enumerate(sessions)}
    cand_by_side: dict[str, list[dict]] = {"high": [], "low": []}
    for cand in earliest.values():
        cand_by_side[cand["side"]].append(cand)

    prev_session: dict[str, str] = {}
    for a, b in zip(sessions, sessions[1:]):
        prev_session[b] = a

    for d in sessions:
        if d not in prev_session:
            continue
        prev = prev_session[d]
        minutes, bars, m5, b5, day = data[d]
        pminutes, pbars = data[prev][0], data[prev][1]

        # per-session level list: (class, side, price, meta)
        levels: list[tuple] = []
        lo = bisect_left(pminutes, RTH_OPEN)
        hi = bisect_right(pminutes, RTH_CLOSE)
        if hi > lo:
            levels.append(("PD", "high", max(pbars[k][1] for k in range(lo, hi)), {}))
            levels.append(("PD", "low", min(pbars[k][2] for k in range(lo, hi)), {}))
        on_h, on_l = None, None
        for k in range(bisect_left(pminutes, 1080), len(pminutes)):
            on_h = pbars[k][1] if on_h is None else max(on_h, pbars[k][1])
            on_l = pbars[k][2] if on_l is None else min(on_l, pbars[k][2])
        for k in range(0, bisect_left(minutes, RTH_OPEN)):
            on_h = bars[k][1] if on_h is None else max(on_h, bars[k][1])
            on_l = bars[k][2] if on_l is None else min(on_l, bars[k][2])
        if on_h is not None:
            levels.append(("ON", "high", on_h, {}))
            levels.append(("ON", "low", on_l, {}))
        if d in pw:
            levels.append(("PW", "high", pw[d][0], {}))
            levels.append(("PW", "low", pw[d][1], {}))
        di = sess_idx[d]
        for side in ("high", "low"):
            for cand in cand_by_side[side]:
                age = di - sess_idx.get(cand["end_date"], -999)
                if not 0 <= age <= MT_MAX_AGE:
                    continue
                if (cand["end_date"], cand["end_minute"]) >= (d, RTH_OPEN):
                    continue  # formed intraday today: not a resting HTF level
                levels.append(("MT", side, cand["price"],
                               {"cand": cand, "age": age}))

        # raw sweeps per level (once per level per session)
        raw: list[dict] = []
        for cls, side, price, meta in levels:
            bm = scan_level(minutes, bars, side, price)
            if bm is None:
                continue
            if cls == "MT":
                touches, dead = mt_status(meta["cand"], candles, (d, bm))
                if dead or touches < MT_MIN_TOUCH:
                    continue
                meta = {"age": meta["age"], "touches": touches}
            raw.append({"session": d, "cls": cls, "side": side, "price": price,
                        "breach_minute": bm, **meta})
            counts[f"raw_{cls}"] += 1

        # confluence merge: same side, same breach bar, prices within T_TOL
        raw.sort(key=lambda e: (e["side"], e["breach_minute"],
                                -CLASS_RANK[e["cls"]], e["price"]))
        merged: list[dict] = []
        for e in raw:
            m0 = merged[-1] if merged else None
            if (m0 and m0["side"] == e["side"] and m0["breach_minute"] == e["breach_minute"]
                    and abs(m0["price"] - e["price"]) <= T_TOL):
                m0["confluence"] = True
                m0["confluent_classes"] = sorted(set(m0.get("confluent_classes", [m0["cls"]]) + [e["cls"]]))
                counts["confluent_absorbed"] += 1
                continue
            e["confluence"] = False
            merged.append(e)

        # verdicts + forward panels
        for e in merged:
            e["sign"] = 1 if e["side"] == "high" else -1
            sweep_close = 570 + 5 * ((e["breach_minute"] - 570) // 5 + 1)
            e["sweep_close_minute"] = sweep_close
            j0 = bisect_left(m5, sweep_close)
            if j0 >= len(m5) or m5[j0] != sweep_close or j0 + K + max(HORIZONS) >= len(m5):
                counts["dropped_no_bars"] += 1
                e["verdict"] = "DROPPED"
                events.append(e)
                continue
            closes = [b[3] for b in b5]
            level, sign = e["price"], e["sign"]
            # depth: max penetration, sweep bar + K verdict bars
            if e["side"] == "high":
                depth = max(b5[j][1] - level for j in range(j0, j0 + K + 1))
            else:
                depth = max(level - b5[j][2] for j in range(j0, j0 + K + 1))
            e["depth_pts"] = round(depth, 2)
            # verdict-bar records, breach-signed forward panels from each bar
            vb: list[dict] = []
            for j in range(j0 + 1, j0 + 1 + K):
                inside = closes[j] < level if e["side"] == "high" else closes[j] > level
                f1, q70 = f1_at(day, m5[j])
                vb.append({
                    "close_minute": m5[j], "inside": inside,
                    "f1_5": f1, "q70": q70,
                    "fwd": {h: round(sign * (closes[j + h] - closes[j]), 2)
                            for h in HORIZONS},
                })
            e["bars"] = vb
            e["fwd_sweep"] = {h: round(sign * (closes[j0 + h] - closes[j0]), 2)
                              for h in HORIZONS}
            e["has_flow"] = all(b["f1_5"] is not None for b in vb)
            reclaim_j = next((i for i, b in enumerate(vb) if b["inside"]), None)
            e["reclaimed"] = reclaim_j is not None
            e["reclaim_j"] = reclaim_j
            e["verdict"], e["verdict_j"] = classify(e, [b["f1_5"] for b in vb])
            events.append(e)
    return events, counts


def classify(e: dict, f1s: list[float | None]) -> tuple[str, int | None]:
    """Verdict from an event's verdict-bar flow values (spec section 2)."""
    if not e["has_flow"]:
        return "NO_FLOW", None
    sign = e["sign"]
    q70 = e["bars"][0]["q70"]
    if e["reclaimed"]:
        j = e["reclaim_j"]
        f1 = f1s[j]
        if f1 * sign < 0 and abs(f1) >= q70:
            return "REVERSAL", j
        return "NEITHER", K - 1
    with_breach = sum(1 for f1 in f1s if f1 * sign > 0 and abs(f1) >= q70)
    if with_breach >= 2:
        return "CONTINUATION", K - 1
    return "NEITHER", K - 1


def trade_fwd(e: dict, verdict: str, j: int) -> dict[int, float]:
    """Trade-direction forward points from verdict bar j's close."""
    s = -1 if verdict == "REVERSAL" else 1
    return {h: s * e["bars"][j]["fwd"][h] for h in HORIZONS}


# ---------------------------------------------------------------- panels


def t_stat(values: list[float]) -> float:
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    sd = statistics.stdev(values)
    return mean / (sd / math.sqrt(n)) if sd else 0.0


def fwd_panel(rows: list[dict[int, float]]) -> dict:
    out = {"n": len(rows)}
    for h in HORIZONS:
        vals = [r[h] for r in rows]
        if vals:
            out[f"h{h}"] = {
                "mean_pts": round(statistics.mean(vals), 2),
                "t": round(t_stat(vals), 2),
                "ev_usd": round((statistics.mean(vals) - TICK) * POINT_VALUE - RT_COST, 2),
            }
    return out


def monthly_median(sessions: list[str], stamps: list[str]) -> tuple[float, dict]:
    months = sorted({s[:7] for s in sessions})
    per = {m: 0 for m in months}
    for s in stamps:
        per[s[:7]] += 1
    return statistics.median(per.values()), per


# ---------------------------------------------------------------- controls


def day_shuffle(entries: list[tuple], data: dict[str, tuple], sessions: list[str],
                n_perm: int, seed: int) -> dict:
    """(verdict close minute, trade sign) pairs replayed on random other
    sessions; fwd-6 mean distribution vs observed (placement control)."""
    if not entries:
        return {"n": 0}
    rng = random.Random(seed)
    observed = statistics.mean(v for _, _, v in entries)
    perms = []
    for _ in range(n_perm):
        vals = []
        for minute, tsign, _ in entries:
            s = rng.choice(sessions)
            m5, b5 = data[s][2], data[s][3]
            j = bisect_left(m5, minute)
            if j < len(m5) and m5[j] == minute and j + 6 < len(m5):
                vals.append(tsign * (b5[j + 6][3] - b5[j][3]))
        perms.append(statistics.mean(vals) if vals else 0.0)
    return {
        "n": len(entries), "n_permutations": n_perm,
        "observed_fwd6_pts": round(observed, 2),
        "perm_mean": round(statistics.mean(perms), 2),
        "perm_sd": round(statistics.stdev(perms), 2),
        "p_value": round(sum(p >= observed for p in perms) / n_perm, 3),
    }


def sign_scramble(events: list[dict], n_perm: int, seed: int) -> dict:
    """Flip each verdict bar's F1_5 sign w.p. 0.5, reclassify, and measure
    each branch's fwd-6 trade-direction mean.  Destroys directional flow
    information, keeps magnitudes, reclaim geometry, and placement."""
    rng = random.Random(seed)
    usable = [e for e in events if e.get("has_flow")]

    def branch_means(flip: bool) -> dict[str, float | None]:
        rows = {"REVERSAL": [], "CONTINUATION": []}
        for e in usable:
            f1s = [b["f1_5"] * (-1 if flip and rng.random() < 0.5 else 1)
                   for b in e["bars"]]
            v, j = classify(e, f1s)
            if v in rows:
                rows[v].append(trade_fwd(e, v, j)[6])
        return {k: (statistics.mean(v) if v else None) for k, v in rows.items()}

    obs = branch_means(flip=False)
    perms = {"REVERSAL": [], "CONTINUATION": []}
    for _ in range(n_perm):
        pm = branch_means(flip=True)
        for k in perms:
            perms[k].append(pm[k] if pm[k] is not None else 0.0)
    out = {"n_permutations": n_perm}
    for k in perms:
        if obs[k] is None:
            out[k] = {"observed": None}
            continue
        out[k] = {
            "observed_fwd6_pts": round(obs[k], 2),
            "perm_mean": round(statistics.mean(perms[k]), 2),
            "perm_sd": round(statistics.stdev(perms[k]), 2),
            "p_value": round(sum(p >= obs[k] for p in perms[k]) / n_perm, 3),
        }
    return out


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("var/sfb"))
    parser.add_argument("--permutations", type=int, default=200)
    args = parser.parse_args()

    fomc = fomc_dates()
    sessions = sorted(p.stem for p in FIX.glob("*.jsonl"))
    print(f"{len(sessions)} sessions {sessions[0]}..{sessions[-1]}")
    data: dict[str, tuple] = {}
    for s in sessions:
        minutes, bars = load_bars(FIX / f"{s}.jsonl")
        if not minutes:
            continue
        m5, b5 = rth_5m(minutes, bars)
        if len(m5) < 40:
            continue
        data[s] = (minutes, bars, m5, b5, flow_day(s))
    sessions = sorted(data)
    print(f"{len(sessions)} usable, flow on {sum(1 for s in sessions if data[s][4])}")

    candles = build_candles(sessions, data)
    pw = weekly_extremes(sessions, data)
    print(f"{len(candles)} 4H candles, PW defined for {len(pw)} sessions")

    events, counts = find_events(sessions, data, candles, pw)
    live = [e for e in events if e["verdict"] != "DROPPED"]
    print(f"{len(events)} events ({counts['dropped_no_bars']} dropped at day edge)")

    # FOMC exclusion for branch/frequency cells
    def fomc_ok(e: dict) -> bool:
        if e["session"] not in fomc:
            return True
        j = e["verdict_j"] if e["verdict_j"] is not None else K - 1
        return e["bars"][j]["close_minute"] <= FOMC_CUTOFF

    branch = {v: [e for e in live if e["verdict"] == v and fomc_ok(e)]
              for v in ("REVERSAL", "CONTINUATION", "NEITHER")}
    counts["fomc_excluded"] = sum(
        1 for e in live if e["verdict"] in ("REVERSAL", "CONTINUATION") and not fomc_ok(e))

    # 1. frequency, first -- branch verdicts require flow, so the gate is
    # computed over flow-covered months only (the walk-forward model warmup
    # makes the first ~2.5 months structural zeros, not signal)
    flow_sessions = [s for s in sessions if data[s][4] is not None]
    med_all, per_month_all = monthly_median(sessions, [e["session"] for e in live])
    combined = branch["REVERSAL"] + branch["CONTINUATION"]
    med_branch, per_month_branch = monthly_median(flow_sessions,
                                                  [e["session"] for e in combined])
    frequency = {
        "events_per_month_median": med_all,
        "events_by_class": {c: sum(1 for e in live if e["cls"] == c)
                            for c in CLASS_RANK},
        "verdict_counts": {v: len(branch[v]) for v in branch}
        | {"NO_FLOW": sum(1 for e in live if e["verdict"] == "NO_FLOW")},
        "flow_span": [flow_sessions[0], flow_sessions[-1], len(flow_sessions)],
        "combined_branch_per_month_median_flow_months": med_branch,
        "combined_branch_per_month": per_month_branch,
        "gate_pass": med_branch >= GATE_FREQ,
    }

    # 2. hierarchy panels (descriptive: all live events, breach-signed from sweep close)
    hierarchy = {}
    for c in CLASS_RANK:
        rows = [e for e in live if e["cls"] == c]
        hierarchy[c] = {
            "n": len(rows),
            "confluent": sum(1 for e in rows if e["confluence"]),
            "reclaim_rate_K": round(statistics.mean([e["reclaimed"] for e in rows]), 4) if rows else None,
            "median_depth_pts": round(statistics.median([e["depth_pts"] for e in rows]), 2) if rows else None,
            "fwd_from_sweep_breach_signed": fwd_panel([e["fwd_sweep"] for e in rows]),
        }
    mt_rows = [e for e in live if e["cls"] == "MT"]
    hierarchy["MT_by_touches"] = {
        "2": fwd_panel([e["fwd_sweep"] for e in mt_rows if e.get("touches") == 2]),
        "3+": fwd_panel([e["fwd_sweep"] for e in mt_rows if e.get("touches", 0) >= 3]),
    }

    # 3. verdict separation (the go/no-go)
    verdict = {}
    for v in ("REVERSAL", "CONTINUATION", "NEITHER"):
        rows = [trade_fwd(e, e["verdict"], e["verdict_j"]) for e in branch[v]]
        panel = fwd_panel(rows)
        verdict[v] = panel
        verdict[v + "_by_class"] = {
            c: fwd_panel([trade_fwd(e, e["verdict"], e["verdict_j"])
                          for e in branch[v] if e["cls"] == c])
            for c in CLASS_RANK}
    def gate_branch(v: str) -> dict:
        p = verdict[v]
        pos_t = [h for h in HORIZONS
                 if p.get(f"h{h}", {}).get("mean_pts", 0) > 0
                 and p.get(f"h{h}", {}).get("t", 0) >= GATE_T]
        return {"n": p["n"], "n_ok": p["n"] >= GATE_N,
                "horizons_clearing": pos_t, "t_ok": len(pos_t) >= 2}
    neither_flat = all(abs(verdict["NEITHER"].get(f"h{h}", {}).get("t", 0)) < GATE_T
                       for h in HORIZONS)
    gates = {
        "frequency": frequency["gate_pass"],
        "REVERSAL": gate_branch("REVERSAL"),
        "CONTINUATION": gate_branch("CONTINUATION"),
        "neither_flat": neither_flat,
    }
    gates["go"] = (frequency["gate_pass"] and neither_flat
                   and all(gates[v]["n_ok"] and gates[v]["t_ok"]
                           for v in ("REVERSAL", "CONTINUATION")))

    # 4. controls
    naked = {}
    for c in CLASS_RANK:
        rows = [e for e in live if e["cls"] == c]
        fades = [{h: -e["bars"][e["reclaim_j"]]["fwd"][h] for h in HORIZONS}
                 for e in rows if e["reclaimed"]]
        conts = [{h: e["bars"][K - 1]["fwd"][h] for h in HORIZONS}
                 for e in rows if not e["reclaimed"]]
        naked[c] = {"reclaim_fade": fwd_panel(fades),
                    "no_reclaim_continuation": fwd_panel(conts)}
    shuffles = {}
    for v in ("REVERSAL", "CONTINUATION"):
        entries = [(e["bars"][e["verdict_j"]]["close_minute"],
                    -e["sign"] if v == "REVERSAL" else e["sign"],
                    trade_fwd(e, v, e["verdict_j"])[6]) for e in branch[v]]
        shuffles[v] = day_shuffle(entries, data, sessions, 100, 20260820 + CLASS_RANK.get(v, 0))
    scramble = sign_scramble([e for e in live if fomc_ok(e)], args.permutations, 20260820)

    out = {
        "meta": {
            "run_date": "2026-08-20", "spec": "SFB v1.0 Phase 1",
            "sessions": [sessions[0], sessions[-1], len(sessions)],
            "params": {"B_ticks": 4, "T_tol_ticks": 10, "K": K,
                       "inside_min": INSIDE_MIN, "window": [SWEEP_FIRST, SWEEP_LAST],
                       "mt_max_age": MT_MAX_AGE, "mt_min_touch": MT_MIN_TOUCH,
                       "horizons": list(HORIZONS)},
            "journal_counts": dict(counts),
        },
        "frequency": frequency,
        "hierarchy": hierarchy,
        "verdict": verdict,
        "gates": gates,
        "controls": {"naked_by_class": naked, "day_shuffle": shuffles,
                     "f1_sign_scramble": scramble},
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "phase1.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    with (args.out / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    print("\n=== 1. frequency ===")
    print(json.dumps(frequency, indent=2))
    print("\n=== 2. hierarchy ===")
    print(json.dumps(hierarchy, indent=2))
    print("\n=== 3. verdict separation ===")
    print(json.dumps(verdict, indent=2))
    print("\n=== gates ===")
    print(json.dumps(gates, indent=2))
    print("\n=== 4. controls ===")
    print(json.dumps(out["controls"], indent=2))
    print(f"\nwritten -> {args.out}")


if __name__ == "__main__":
    main()
