"""SDD Phase 1: SMT divergence x delta failure x displacement, $0 on existing data.

Program 12 (spec SDD.md v1.0).  NQ prints an N-session extreme that ES
does not confirm (the trap candidate), three orderflow conditions each
do one non-overlapping job, and the trigger is a displacement AGAINST
the swept extreme -- entry AT the displacement close, never on a
retracement (dead family, locked).

Definitions implemented (5m RTH bars, walk-forward, window 09:35-14:30
for both the extreme and the displacement bar):

  extreme    NQ 5m bar whose high exceeds max(prior N-1 sessions' RTH
             high, today's running high) -- N = 2 headline, N = 1
             companion sweep.  Consecutive new-extreme prints collapse
             to the last one before the trigger (supersession); a prior
             print still counts as "prior extreme" for delta_fail.
  SMT        at the NQ extreme bar +-1 bar, ES has not exceeded its own
             corresponding trailing extreme (same N-window, ES ref
             excludes the tolerance bars).  ES session missing -> event
             excluded, journaled.
  thin (#1)  extreme bar: F4 large-lot share <= trailing-60-session
             median of all RTH bar large_share AND |F1_5| < day Q70.
             Both halves journaled separately.
  delta_fail session cumulative delta (var/flow buy-sell, prefix sum
     (#2)    from 09:30) at the extreme bar fails to confirm vs the
             latest prior same-side extreme >= 6 bars earlier in-session
             (price HH with CD not higher; mirror for lows).  No prior
             extreme -> undefined, journaled.
  displacement  within W = 6 bars after the extreme: opposite-direction
             5m bar, range >= 1.5x trailing-20-bar median (>= 5 trail
             bars), |F1_5| >= Q70 with the bar's direction.  First
             qualifying bar is the trigger; a newer same-side extreme
             before it supersedes the event.
  disp_q (#4) displacement bar large_share >= trailing-60-session
             median; raw share + bar delta + trailing-quartile journaled
             per event for the gradient.

Feature gating, pre-declared: SDD uses no model outputs -- f1_5, q_f1,
large_share, and per-minute delta are all model-free, so the event base
is the full fixture year (unlike SFB's model-gated span).  The
trailing-60-session large_share median requires >= 20 trailing
sessions; earlier events carry thin/disp_q = undefined (journaled).

Phase 1 is bracket-free: forward returns at 1/3/6/13 5m bars in the
displacement direction from the displacement close; EV$ = (pts - 1 tick
adverse) * $20 - $10 RT; hit = share of events with positive forward
points.  Headline horizon for EV/hit/bootstrap comparisons: h6, with
h13 companion (declared here, before results).  FOMC sessions: entries
after 13:00 ET excluded from cells (journaled).  Cap 2/day journaled,
not enforced (Phase 1 measures events).

Six declared cells (one look, disclosed): BASE (SMT + displacement),
BASE+#1, BASE+#2, BASE+#4, COMBO-A (#1+#2), COMBO-B (#1+#2+#4).
"Adds" margin vs BASE, pre-registered: EV +$50/trade or hit +4 pts at
h6 or h13 with random-subset p < 0.10.  Proceed-gate: some cell with
fwd positive at >= 2 horizons T >= 1.5, n >= 50, cell frequency >= 8
per month, controls behaving.

Priors, recorded before results (2026-08-20, from the spec):
  1. The design fades extremes -> nine dead fade/chase families against
     it; the only rescue is that entry is WITH a displacement.
  2. Frequency is the second risk: three conditions on divergence
     events; combos can starve.
  3. FRB-1's LL overlay anomaly leans in SMT's favor (+$965 vs $230,
     n = 32, recorded not promoted); the honest retest is section 3.
  4. F2-at-extremes failed standalone (DDR); untested as a coincident
     condition.  Flow-as-confirmer is dead three studies deep; both
     flow conditions here are coincident measurements.

v1.1 amendment (2026-08-20, --trigger state): SDD's first and only
permitted trigger redesign.  The 6-bar window is replaced by an
SMT-live STATE: the flag raises when an SMT-divergent extreme prints
(resolution at the tolerance bar's close) and dies on the first of
reclaim (NQ prints beyond the level -- a divergent re-print REPLACES
the flag instead), ES confirmation (ES prints its own new N-window
extreme after the tolerance bar), or session end.  Any qualifying
displacement (v1.0 definition) completing while the opposing flag is
up is an entry; the first consumes the flag (rank journaled, so later
displacements are measurable).  Four declared cells: BASE-S,
BASE-S+disp_q, CONF-S (identical machinery on ES-confirmed extremes,
minus the inapplicable ES-confirmation kill -- asymmetry disclosed),
and the fc_t13 smt_live column.  delta_fail and thin are removed per
the amendment's decision record.  Time-since-extreme gradient
(<=30 / 30-90 / >90 min) pre-declared.  Frequency gate evaluated per
N sweep independently; a pass at either N proceeds with that N as the
design (declared here, before results).  fc_t13 column: develop
journal trades tagged side-matched / opposed / both / none by live
smt flags at the entry minute; contrast = EV(matched) - EV(none),
gate >= +$150 with n >= 40 matched; shadow trades postdate fixture
coverage (> 2026-08-12) and are reported untaggable -- the column
rests on the develop journal, disclosed.

Usage:
  uv run python scripts/sdd_study.py [--trigger window|state] [--out var/sdd]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from bisect import bisect_left, bisect_right
from datetime import datetime
from itertools import accumulate
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FIX_NQ = Path("var/fixtures/1m-full")
FIX_ES = Path("var/fixtures/es-1m")
DECISIONS = Path("var/decisions/k3m")
FLOW = Path("var/flow")
FOMC = Path("config/fomc_dates.json")

POINT_VALUE = 20.0
RT_COST = 10.0
TICK = 0.25
W = 6                                # displacement window, 5m bars
DISP_MULT = 1.5
DISP_TRAIL = 20
MIN_TRAIL = 5
DF_MIN_GAP = 6                       # delta_fail: prior extreme >= 6 bars earlier
LS_TRAIL_SESS = 60                   # large_share trailing median window
LS_MIN_SESS = 20
RTH_OPEN, RTH_CLOSE = 570, 959
EXT_FIRST, EXT_LAST = 575, 870       # extreme AND displacement bar closes
FOMC_CUTOFF = 780
HORIZONS = (1, 3, 6, 13)
HEADLINE_H = 6
GATE_T = 1.5
GATE_N = 50
GATE_FREQ = 8.0
MARGIN_EV = 50.0
MARGIN_HIT = 0.04
N_SWEEP = (2, 1)                     # headline first


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


def features_day(session: str) -> dict | None:
    """f1_5/large_share per 5m bar + q70 -- model-free, so no model gate."""
    path = DECISIONS / f"{session}.json"
    if not path.exists():
        return None
    day = json.loads(path.read_text())
    if not day.get("q_f1"):
        return None
    q70 = float(day["q_f1"].get("70", "nan"))
    if q70 != q70:
        return None
    return {"q70": q70, "bars": day.get("bars", {})}


def cd_by_bar(session: str, m5: list[int]) -> list[float] | None:
    """Session cumulative delta (buy - sell) at each 5m close."""
    path = FLOW / f"{session}.json"
    if not path.exists():
        return None
    mins = json.loads(path.read_text())["minutes"]
    deltas = [mins.get(str(k), {}).get("buy", 0) - mins.get(str(k), {}).get("sell", 0)
              for k in range(1, 391)]
    prefix = list(accumulate(deltas))
    return [prefix[min(m - 570, 390) - 1] for m in m5]


def fomc_dates() -> set[str]:
    return set(json.loads(FOMC.read_text())["dates"])


# ---------------------------------------------------------------- stats


def t_stat(values: list[float]) -> float:
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    sd = statistics.stdev(values)
    return mean / (sd / math.sqrt(n)) if sd else 0.0


def fwd_panel(rows: list[dict]) -> dict:
    out = {"n": len(rows)}
    for h in HORIZONS:
        vals = [r["fwd"][h] for r in rows]
        if vals:
            out[f"h{h}"] = {
                "mean_pts": round(statistics.mean(vals), 2),
                "t": round(t_stat(vals), 2),
                "hit": round(sum(v > 0 for v in vals) / len(vals), 4),
                "ev_usd": round((statistics.mean(vals) - TICK) * POINT_VALUE - RT_COST, 2),
            }
    return out


def vs_base(cell: list[dict], base: list[dict], n_draw: int, seed: int) -> dict:
    """Random same-size subsets of BASE vs the cell mean/hit (selection test)."""
    out = {}
    rng = random.Random(seed)
    idx = list(range(len(base)))
    for h in (HEADLINE_H, 13):
        cv = [r["fwd"][h] for r in cell]
        bv = [r["fwd"][h] for r in base]
        if len(cv) < 3 or len(cv) >= len(bv):
            out[f"h{h}"] = None
            continue
        obs_ev = statistics.mean(cv)
        obs_hit = sum(v > 0 for v in cv) / len(cv)
        ge_ev = ge_hit = 0
        for _ in range(n_draw):
            sub = [bv[i] for i in rng.sample(idx, len(cv))]
            if statistics.mean(sub) >= obs_ev:
                ge_ev += 1
            if sum(v > 0 for v in sub) / len(sub) >= obs_hit:
                ge_hit += 1
        out[f"h{h}"] = {
            "d_ev_usd": round((obs_ev - statistics.mean(bv)) * POINT_VALUE, 2),
            "d_hit": round(obs_hit - sum(v > 0 for v in bv) / len(bv), 4),
            "p_ev": round(ge_ev / n_draw, 3),
            "p_hit": round(ge_hit / n_draw, 3),
        }
    return out


def monthly_median(months: list[str], stamps: list[str]) -> float:
    per = {m: 0 for m in months}
    for s in stamps:
        per[s[:7]] += 1
    return statistics.median(per.values())


# ---------------------------------------------------------------- events


def extreme_bars(b5: list[tuple], last_idx: int, side: str, ref_prior: float | None) -> list[int]:
    """Walk-forward new-extreme bar indices (window-capped)."""
    out: list[int] = []
    run = ref_prior if ref_prior is not None else None
    for j in range(0, last_idx + 1):
        x = b5[j][1] if side == "high" else b5[j][2]
        if run is not None:
            better = x > run if side == "high" else x < run
            if better:
                out.append(j)
        run = x if run is None else (max(run, x) if side == "high" else min(run, x))
    return out


def scan_session(ctx: dict) -> list[dict]:
    """All (extreme, displacement) events for one session and one N."""
    m5, b5 = ctx["m5"], ctx["b5"]
    es = ctx["es"]                      # None or (minute->index, m5e, b5e)
    feats, cd = ctx["feats"], ctx["cd"]
    med60, q_ls = ctx["med60"], ctx["q_ls"]
    ranges = [b[1] - b[2] for b in b5]
    last_ext = bisect_right(m5, EXT_LAST) - 1
    events: list[dict] = []

    def bar_feat(j: int) -> tuple[float | None, float | None]:
        rec = feats["bars"].get(str(m5[j] - 570)) if feats else None
        if not isinstance(rec, dict):
            return None, None
        return rec.get("f1_5"), rec.get("large_share")

    for side, dsign in (("high", -1), ("low", 1)):
        ref = ctx["ref_prior"][side] if ctx["n_win"] == 2 else None
        exts = extreme_bars(b5, last_ext, side, ref)
        if ctx["n_win"] == 1 and exts and exts[0] == 0:
            exts = exts[1:]             # bar 0 vs empty window is not an extreme
        for pos, j in enumerate(exts):
            if m5[j] < EXT_FIRST:
                continue
            j2 = exts[pos + 1] if pos + 1 < len(exts) else None
            disp = None
            superseded = False
            for d in range(j + 1, min(j + W, last_ext) + 1):
                if j2 is not None and d >= j2:
                    superseded = True
                    break
                o, h, l, c = b5[d]
                right_dir = c < o if side == "high" else c > o
                if not right_dir:
                    continue
                trail = ranges[max(0, d - DISP_TRAIL):d]
                if len(trail) < MIN_TRAIL or (h - l) < DISP_MULT * statistics.median(trail):
                    continue
                f1_d, ls_d = bar_feat(d)
                if f1_d is None or feats is None:
                    continue
                if f1_d * dsign <= 0 or abs(f1_d) < feats["q70"]:
                    continue
                if d + max(HORIZONS) >= len(m5):
                    break               # no room for the forward panel
                disp = d
                break
            # superseded prints stay in the event base (funnel + extreme-
            # anchored panels) with no displacement attached: dropping them
            # would condition the extreme-anchored panels on price NOT
            # exceeding the extreme afterward -- look-ahead survivorship.
            # Displacement cells are unaffected (resolved by entry time).

            # SMT at j (+-1 bar tolerance)
            smt = None
            if es is not None:
                es_idx, m5e, b5e = es
                tol = [k for k in (j - 1, j, j + 1)
                       if 0 <= k < len(m5) and m5[k] in es_idx]
                es_ref = ctx["es_ref_prior"][side] if ctx["n_win"] == 2 else None
                for k in range(0, j - 1):
                    if m5[k] not in es_idx:
                        continue
                    x = b5e[es_idx[m5[k]]][1] if side == "high" else b5e[es_idx[m5[k]]][2]
                    es_ref = x if es_ref is None else (
                        max(es_ref, x) if side == "high" else min(es_ref, x))
                if es_ref is not None and tol:
                    if side == "high":
                        conf = max(b5e[es_idx[m5[k]]][1] for k in tol) > es_ref
                    else:
                        conf = min(b5e[es_idx[m5[k]]][2] for k in tol) < es_ref
                    smt = not conf

            f1_j, ls_j = bar_feat(j)
            thin_size = ls_j <= med60 if (ls_j is not None and med60 is not None) else None
            thin_f1 = abs(f1_j) < feats["q70"] if (f1_j is not None and feats) else None
            thin = (thin_size and thin_f1) if (thin_size is not None and thin_f1 is not None) else None

            dfail, df_prior_j = None, None
            if cd is not None:
                prior = [p for p in exts[:pos] if j - p >= DF_MIN_GAP]
                if prior:
                    df_prior_j = prior[-1]
                    dfail = (cd[j] < cd[df_prior_j] if side == "high"
                             else cd[j] > cd[df_prior_j])

            e = {
                "session": ctx["session"], "n_win": ctx["n_win"], "side": side,
                "ext_j": j, "ext_minute": m5[j],
                "ext_price": b5[j][1] if side == "high" else b5[j][2],
                "smt": smt, "thin": thin, "thin_size": thin_size, "thin_f1": thin_f1,
                "delta_fail": dfail, "df_prior_j": df_prior_j,
                "superseded": superseded, "disp_j": disp,
            }
            if disp is not None:
                f1_d, ls_d = bar_feat(disp)
                dq = ls_d >= med60 if (ls_d is not None and med60 is not None) else None
                quart = None
                if ls_d is not None and q_ls is not None:
                    quart = sum(ls_d >= q for q in q_ls) + 1     # 1..4
                closes = [b[3] for b in b5]
                e.update({
                    "disp_minute": m5[disp], "dir": dsign,
                    "disp_f1": f1_d, "disp_large_share": ls_d,
                    "disp_delta_q": dq, "disp_ls_quartile": quart,
                    "stop_dist_pts": round(abs(closes[disp] - e["ext_price"]) + TICK, 2),
                    "fwd": {h: round(dsign * (closes[disp + h] - closes[disp]), 2)
                            for h in HORIZONS},
                })
            events.append(e)
    return events


# ---------------------------------------------------------------- v1.1 state


JOURNALS = [("develop", Path("var/tfr/fc_t13/journal")),
            ("shadow", Path("var/shadow/journal"))]
TIME_BUCKETS = ((0, 30, "<=30m"), (30, 90, "30-90m"), (90, 10_000, ">90m"))
SEP_P = 0.05
FC_GATE_EV = 150.0
FC_GATE_N = 40


def smt_of_print(j: int, side: str, m5: list[int], b5: list[tuple],
                 esx, es_ref_prior: float | None, n_win: int) -> bool | None:
    """v1.0 SMT status of an NQ extreme print (+-1 bar ES tolerance)."""
    if esx is None or j + 1 >= len(m5):
        return None
    es_idx, m5e, b5e = esx
    tol = [k for k in (j - 1, j, j + 1) if 0 <= k < len(m5) and m5[k] in es_idx]
    es_ref = es_ref_prior if n_win == 2 else None
    for k in range(0, j - 1):
        if m5[k] not in es_idx:
            continue
        x = b5e[es_idx[m5[k]]][1] if side == "high" else b5e[es_idx[m5[k]]][2]
        es_ref = x if es_ref is None else (
            max(es_ref, x) if side == "high" else min(es_ref, x))
    if es_ref is None or not tol:
        return None
    if side == "high":
        return not (max(b5e[es_idx[m5[k]]][1] for k in tol) > es_ref)
    return not (min(b5e[es_idx[m5[k]]][2] for k in tol) < es_ref)


def scan_session_state(ctx: dict) -> tuple[list[dict], list[dict]]:
    """v1.1 flag state machine: one flag per side (kind smt|conf), kills by
    reclaim / replacement / ES confirmation / session end; every qualifying
    displacement against a live flag is an event (rank 1 consumes)."""
    m5, b5 = ctx["m5"], ctx["b5"]
    esx, feats = ctx["es"], ctx["feats"]
    med60, q_ls, n_win = ctx["med60"], ctx["q_ls"], ctx["n_win"]
    closes = [b[3] for b in b5]
    ranges = [b[1] - b[2] for b in b5]
    events: list[dict] = []
    flags_out: list[dict] = []

    def bar_feat(j: int) -> tuple[float | None, float | None]:
        rec = feats["bars"].get(str(m5[j] - 570)) if feats else None
        if not isinstance(rec, dict):
            return None, None
        return rec.get("f1_5"), rec.get("large_share")

    # qualifying displacement direction per window bar (v1.0 definition)
    disp_dir: dict[int, int] = {}
    if feats:
        for j in range(len(m5)):
            if not EXT_FIRST <= m5[j] <= EXT_LAST:
                continue
            o, h, l, c = b5[j]
            d = -1 if c < o else (1 if c > o else 0)
            if d == 0:
                continue
            trail = ranges[max(0, j - DISP_TRAIL):j]
            if len(trail) < MIN_TRAIL or (h - l) < DISP_MULT * statistics.median(trail):
                continue
            f1, _ = bar_feat(j)
            if f1 is None or f1 * d <= 0 or abs(f1) < feats["q70"]:
                continue
            disp_dir[j] = d

    # full-session extreme prints, both instruments
    last = len(m5) - 1
    nq_prints = {side: set(extreme_bars(b5, last, side, ctx["ref_prior"][side]))
                 for side in ("high", "low")}
    es_kill: dict[str, set] = {"high": set(), "low": set()}
    if esx is not None:
        es_idx, m5e, b5e = esx
        minute_to_j = {m: j for j, m in enumerate(m5)}
        for side in ("high", "low"):
            ref = ctx["es_ref_prior"][side] if n_win == 2 else None
            eps = extreme_bars(b5e, len(m5e) - 1, side, ref)
            if n_win == 1 and eps and eps[0] == 0:
                eps = eps[1:]
            es_kill[side] = {minute_to_j[m5e[k]] for k in eps if m5e[k] in minute_to_j}
    for side in ("high", "low"):
        if n_win == 1:
            ps = sorted(nq_prints[side])
            if ps and ps[0] == 0:
                nq_prints[side].discard(0)

    flag: dict[str, dict | None] = {"high": None, "low": None}

    def kill(side: str, cause: str, j: int) -> None:
        fl = flag[side]
        flags_out.append({
            "session": ctx["session"], "n_win": n_win, "side": side,
            "kind": fl["kind"], "level": fl["level"],
            "raise_minute": fl["raise_minute"], "kill_minute": m5[j],
            "duration_min": m5[j] - fl["raise_minute"], "cause": cause,
            "n_disp": fl["n_disp"],
        })
        flag[side] = None

    for j in range(len(m5)):
        for side in ("high", "low"):
            fl = flag[side]
            if fl and fl["kind"] == "smt" and j in es_kill[side] and j > fl["ext_j"] + 1:
                kill(side, "es_confirm", j)
                fl = None
            if j in nq_prints[side]:
                stt = None
                if EXT_FIRST <= m5[j] <= EXT_LAST:
                    stt = smt_of_print(j, side, m5, b5, esx,
                                       ctx["es_ref_prior"][side], n_win)
                if fl:
                    kill(side, "replaced" if stt is not None else "reclaim", j)
                if stt is not None:
                    flag[side] = {
                        "kind": "smt" if stt else "conf",
                        "level": b5[j][1] if side == "high" else b5[j][2],
                        "ext_j": j, "raise_minute": m5[j], "n_disp": 0,
                    }
        d = disp_dir.get(j, 0)
        if d:
            side = "high" if d == -1 else "low"
            fl = flag[side]
            if fl and j > fl["ext_j"] and j + max(HORIZONS) < len(m5):
                fl["n_disp"] += 1
                f1_d, ls_d = bar_feat(j)
                dq = ls_d >= med60 if (ls_d is not None and med60 is not None) else None
                quart = (sum(ls_d >= q for q in q_ls) + 1
                         if ls_d is not None and q_ls is not None else None)
                events.append({
                    "session": ctx["session"], "n_win": n_win,
                    "kind": fl["kind"], "side": side, "dir": d,
                    "ext_minute": fl["raise_minute"], "level": fl["level"],
                    "disp_minute": m5[j], "rank": fl["n_disp"],
                    "t_since_min": m5[j] - fl["raise_minute"],
                    "disp_large_share": ls_d, "disp_delta_q": dq,
                    "disp_ls_quartile": quart,
                    "stop_dist_pts": round(abs(closes[j] - fl["level"]) + TICK, 2),
                    "fwd": {h: round(d * (closes[j + h] - closes[j]), 2)
                            for h in HORIZONS},
                })
    for side in ("high", "low"):
        if flag[side]:
            kill(side, "session_end", len(m5) - 1)
    return events, flags_out


def load_fc_trades(known_sessions: set[str]) -> tuple[list[dict], dict]:
    trades: list[dict] = []
    counts = {"develop": 0, "shadow": 0, "no_coverage": 0}
    for source, jdir in JOURNALS:
        for path in sorted(jdir.glob("*.jsonl")):
            for line in path.read_text().splitlines():
                if '"position_closed"' not in line:
                    continue
                r = json.loads(line)
                if r.get("event") != "position_closed":
                    continue
                et = datetime.fromisoformat(r["entry_time"]).astimezone(ET)
                session = et.date().isoformat()
                pnl = float(r["realised_pnl"])
                if source == "shadow":
                    pnl *= 2.5 if session >= "2026-08-20" else 10.0   # MNQ -> NQ-equiv
                if session not in known_sessions:
                    counts["no_coverage"] += 1
                    continue
                counts[source] += 1
                trades.append({
                    "source": source, "session": session,
                    "minute": et.hour * 60 + et.minute,
                    "dir": 1 if r["direction"] == "LONG" else -1,
                    "pnl": pnl,
                })
    return trades, counts


def run_state(sessions, nq, es, feats, cds, med60, q_ls, fomc, args) -> None:
    months = sorted({s[:7] for s in sessions})
    all_events: list[dict] = []
    all_flags: list[dict] = []
    for n_win in N_SWEEP:
        for i, s in enumerate(sessions):
            if n_win == 2 and i == 0:
                continue
            ref_prior = {"high": None, "low": None}
            es_ref_prior = {"high": None, "low": None}
            if n_win == 2:
                pb5 = nq[sessions[i - 1]][1]
                ref_prior = {"high": max(b[1] for b in pb5),
                             "low": min(b[2] for b in pb5)}
                if sessions[i - 1] in es:
                    pbe = es[sessions[i - 1]][2]
                    es_ref_prior = {"high": max(b[1] for b in pbe),
                                    "low": min(b[2] for b in pbe)}
            ctx = {"session": s, "n_win": n_win, "m5": nq[s][0], "b5": nq[s][1],
                   "es": es.get(s), "feats": feats[s], "med60": med60[s],
                   "q_ls": q_ls[s], "ref_prior": ref_prior,
                   "es_ref_prior": es_ref_prior}
            evs, fls = scan_session_state(ctx)
            all_events.extend(evs)
            all_flags.extend(fls)

    out: dict = {"meta": {
        "run_date": "2026-08-20", "spec": "SDD v1.1 Phase 1 (state trigger)",
        "sessions": [sessions[0], sessions[-1], len(sessions)],
        "params": {"n_sweep": list(N_SWEEP), "headline_h": HEADLINE_H,
                   "sep_p": SEP_P, "fc_gate": [FC_GATE_EV, FC_GATE_N]},
    }}
    rng = random.Random(20260821)

    for n_win in N_SWEEP:
        key = f"N{n_win}"
        evs = [e for e in all_events if e["n_win"] == n_win]
        fls = [f for f in all_flags if f["n_win"] == n_win]

        def fomc_ok(e: dict) -> bool:
            return e["session"] not in fomc or e["disp_minute"] <= FOMC_CUTOFF

        base = [e for e in evs if e["kind"] == "smt" and e["rank"] == 1 and fomc_ok(e)]
        dq_cell = [e for e in base if e["disp_delta_q"] is True]
        conf = [e for e in evs if e["kind"] == "conf" and e["rank"] == 1 and fomc_ok(e)]
        multi = [e for e in evs if e["kind"] == "smt" and e["rank"] >= 2 and fomc_ok(e)]

        smt_flags = [f for f in fls if f["kind"] == "smt"]
        funnel = {
            "flags_smt": len(smt_flags),
            "flags_conf": sum(1 for f in fls if f["kind"] == "conf"),
            "smt_flag_minutes_total": sum(f["duration_min"] for f in smt_flags),
            "smt_flag_median_duration_min": (statistics.median(
                [f["duration_min"] for f in smt_flags]) if smt_flags else None),
            "kill_causes": {c: sum(1 for f in smt_flags if f["cause"] == c)
                            for c in ("reclaim", "replaced", "es_confirm", "session_end")},
            "conversions_BASE_S": len(base),
            "multi_disp_rank2plus": len(multi),
            "per_month_median": {
                "BASE-S": monthly_median(months, [e["session"] for e in base]),
                "BASE-S+disp_q": monthly_median(months, [e["session"] for e in dq_cell]),
                "CONF-S": monthly_median(months, [e["session"] for e in conf]),
            },
            "fomc_excluded": sum(1 for e in evs if e["kind"] == "smt"
                                 and e["rank"] == 1 and not fomc_ok(e)),
            "sessions_over_cap2": sum(
                1 for s in sessions
                if sum(1 for e in base if e["session"] == s) > 2),
        }

        matrix = {
            "BASE-S": fwd_panel(base),
            "BASE-S+disp_q": fwd_panel(dq_cell),
            "CONF-S": fwd_panel(conf),
            "multi_disp_journal": fwd_panel(multi),
            "time_gradient": {lbl: fwd_panel(
                [e for e in base if lo < e["t_since_min"] <= hi])
                for lo, hi, lbl in TIME_BUCKETS},
            "disp_ls_quartile_gradient": {
                str(q): fwd_panel([e for e in base if e["disp_ls_quartile"] == q])
                for q in (1, 2, 3, 4)},
        }
        matrix["BASE-S+disp_q"]["vs_base"] = vs_base(dq_cell, base, args.draws, 20260830)

        # separation: BASE-S vs CONF-S, label permutation at h6 (gate) + h13
        separation = {}
        for h in (HEADLINE_H, 13):
            bv = [e["fwd"][h] for e in base]
            cv = [e["fwd"][h] for e in conf]
            if len(bv) > 2 and len(cv) > 2:
                obs = statistics.mean(bv) - statistics.mean(cv)
                pool = bv + cv
                ge = 0
                for _ in range(2000):
                    rng.shuffle(pool)
                    if statistics.mean(pool[:len(bv)]) - statistics.mean(pool[len(bv):]) >= obs:
                        ge += 1
                separation[f"h{h}"] = {
                    "base_s_pts": round(statistics.mean(bv), 2),
                    "conf_s_pts": round(statistics.mean(cv), 2),
                    "diff_pts": round(obs, 2), "p_one_sided": round(ge / 2000, 4)}

        # controls: day-shuffle BASE-S; F4-scramble on disp_q
        obs = statistics.mean(e["fwd"][HEADLINE_H] for e in base) if base else 0.0
        perms = []
        for _ in range(100):
            vals = []
            for e in base:
                s = rng.choice(sessions)
                m5s, b5s = nq[s]
                jj = bisect_left(m5s, e["disp_minute"])
                if jj < len(m5s) and m5s[jj] == e["disp_minute"] and jj + HEADLINE_H < len(m5s):
                    vals.append(e["dir"] * (b5s[jj + HEADLINE_H][3] - b5s[jj][3]))
            perms.append(statistics.mean(vals) if vals else 0.0)
        controls = {"day_shuffle_BASE_S": {
            "observed_fwd6_pts": round(obs, 2),
            "perm_mean": round(statistics.mean(perms), 2),
            "perm_sd": round(statistics.stdev(perms), 2),
            "p_value": round(sum(p >= obs for p in perms) / 100, 3)}}
        obs_d = (statistics.mean(e["fwd"][HEADLINE_H] for e in dq_cell)
                 - statistics.mean(e["fwd"][HEADLINE_H] for e in base)
                 if len(dq_cell) > 2 and base else None)
        perm_d = []
        for _ in range(args.permutations):
            rows = []
            for e in base:
                f = feats[e["session"]]
                m60 = med60[e["session"]]
                if f is None or m60 is None:
                    continue
                recs = [b for b in f["bars"].values()
                        if isinstance(b, dict) and b.get("large_share") is not None]
                if rng.choice(recs)["large_share"] >= m60:
                    rows.append(e)
            if len(rows) > 2:
                perm_d.append(statistics.mean(e["fwd"][HEADLINE_H] for e in rows)
                              - statistics.mean(e["fwd"][HEADLINE_H] for e in base))
        controls["f4_scramble_disp_q"] = {
            "observed_delta_pts": round(obs_d, 2) if obs_d is not None else None,
            "perm_mean": round(statistics.mean(perm_d), 2) if perm_d else None,
            "perm_sd": round(statistics.stdev(perm_d), 2) if len(perm_d) > 2 else None,
            "p_value": (round(sum(p >= obs_d for p in perm_d) / len(perm_d), 3)
                        if obs_d is not None and perm_d else None)}

        gates: dict = {"frequency_BASE_S": funnel["per_month_median"]["BASE-S"] >= GATE_FREQ}
        for c, rows in (("BASE-S", base), ("BASE-S+disp_q", dq_cell)):
            p = matrix[c]
            pos_t = [h for h in HORIZONS
                     if p.get(f"h{h}", {}).get("mean_pts", 0) > 0
                     and p.get(f"h{h}", {}).get("t", 0) >= GATE_T]
            gates[c] = {"n": p["n"], "n_ok": p["n"] >= GATE_N,
                        "horizons_clearing": pos_t, "t_ok": len(pos_t) >= 2}
        gates["separation_ok"] = (separation.get(f"h{HEADLINE_H}", {})
                                  .get("p_one_sided", 1.0) < SEP_P)
        gates["proceed"] = (gates["frequency_BASE_S"] and gates["separation_ok"]
                            and any(gates[c]["n_ok"] and gates[c]["t_ok"]
                                    for c in ("BASE-S", "BASE-S+disp_q")))

        out[key] = {"funnel": funnel, "matrix": matrix, "separation": separation,
                    "controls": controls, "gates": gates}

    # fc_t13 smt_live column (both N, develop journal; shadow untaggable)
    trades, tc = load_fc_trades(set(sessions))
    fc_col: dict = {"journal_counts": tc}
    for n_win in N_SWEEP:
        ivals: dict[str, list[tuple]] = {}
        for f in all_flags:
            if f["n_win"] != n_win or f["kind"] != "smt":
                continue
            ivals.setdefault(f["session"], []).append(
                (f["raise_minute"] + 5, f["kill_minute"], f["side"]))
        tags: dict[str, list[float]] = {"matched": [], "opposed": [], "both": [], "none": []}
        for t in trades:
            live = [side for a, k, side in ivals.get(t["session"], ())
                    if a <= t["minute"] < k]
            implied = {-1 if side == "high" else 1 for side in live}
            m, o = t["dir"] in implied, -t["dir"] in implied
            tag = "both" if m and o else "matched" if m else "opposed" if o else "none"
            tags[tag].append(t["pnl"])
        col = {tag: {"n": len(v),
                     "ev": round(statistics.mean(v), 2) if v else None,
                     "t": round(t_stat(v), 2) if v else None}
               for tag, v in tags.items()}
        mt, nn = tags["matched"], tags["none"]
        col["split_matched_minus_none"] = (
            round(statistics.mean(mt) - statistics.mean(nn), 2) if mt and nn else None)
        col["gate"] = (len(mt) >= FC_GATE_N and mt and nn
                       and statistics.mean(mt) - statistics.mean(nn) >= FC_GATE_EV)
        fc_col[f"N{n_win}"] = col
    out["fc_t13_column"] = fc_col

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "phase1_v11.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    with (args.out / "events_v11.jsonl").open("w", encoding="utf-8") as fh:
        for e in all_events:
            fh.write(json.dumps(e) + "\n")
    with (args.out / "flags_v11.jsonl").open("w", encoding="utf-8") as fh:
        for f in all_flags:
            fh.write(json.dumps(f) + "\n")
    for k in ("N2", "N1"):
        print(f"\n===== {k} =====")
        print(json.dumps(out[k]["funnel"], indent=1))
        print(json.dumps({c: {kk: vv for kk, vv in p.items() if kk != "vs_base"}
                          for c, p in out[k]["matrix"].items()
                          if c not in ("time_gradient", "disp_ls_quartile_gradient")},
                         indent=1))
        print(json.dumps(out[k]["separation"], indent=1))
        print(json.dumps(out[k]["gates"], indent=1))
    print(json.dumps(fc_col, indent=1))
    print(f"\nwritten -> {args.out}")


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("var/sdd"))
    parser.add_argument("--permutations", type=int, default=200)
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--trigger", choices=("window", "state"), default="window")
    args = parser.parse_args()

    fomc = fomc_dates()
    sessions = sorted(p.stem for p in FIX_NQ.glob("*.jsonl"))
    nq: dict[str, tuple] = {}
    es: dict[str, tuple] = {}
    for s in sessions:
        m, b = load_bars(FIX_NQ / f"{s}.jsonl")
        m5, b5 = rth_5m(m, b)
        if len(m5) < 40:
            continue
        nq[s] = (m5, b5)
        ep = FIX_ES / f"{s}.jsonl"
        if ep.exists():
            me, be = load_bars(ep)
            m5e, b5e = rth_5m(me, be)
            if len(m5e) >= 40:
                es[s] = ({mm: i for i, mm in enumerate(m5e)}, m5e, b5e)
    sessions = sorted(nq)
    print(f"{len(sessions)} NQ sessions, ES on {len(es)}")

    feats = {s: features_day(s) for s in sessions}
    cds = {s: cd_by_bar(s, nq[s][0]) if feats[s] else None for s in sessions}
    print(f"features on {sum(1 for s in sessions if feats[s])}, CD on "
          f"{sum(1 for s in sessions if cds[s])}")

    # trailing-60-session large_share median + quartiles, walk-forward
    ls_by_sess = []
    for s in sessions:
        f = feats[s]
        ls_by_sess.append([b["large_share"] for b in f["bars"].values()
                           if isinstance(b, dict) and b.get("large_share") is not None]
                          if f else [])
    med60: dict[str, float | None] = {}
    q_ls: dict[str, tuple | None] = {}
    for i, s in enumerate(sessions):
        pool: list[float] = []
        got = 0
        for k in range(i - 1, -1, -1):
            if ls_by_sess[k]:
                pool.extend(ls_by_sess[k])
                got += 1
                if got >= LS_TRAIL_SESS:
                    break
        if got >= LS_MIN_SESS:
            med60[s] = statistics.median(pool)
            qs = statistics.quantiles(pool, n=4)
            q_ls[s] = tuple(qs)
        else:
            med60[s] = None
            q_ls[s] = None

    if args.trigger == "state":
        run_state(sessions, nq, es, feats, cds, med60, q_ls, fomc, args)
        return

    all_events: list[dict] = []
    for n_win in N_SWEEP:
        for i, s in enumerate(sessions):
            ref_prior = {"high": None, "low": None}
            es_ref_prior = {"high": None, "low": None}
            if n_win == 2 and i >= 1:
                pm5, pb5 = nq[sessions[i - 1]]
                ref_prior = {"high": max(b[1] for b in pb5),
                             "low": min(b[2] for b in pb5)}
                if sessions[i - 1] in es:
                    _, _, pbe = es[sessions[i - 1]]
                    es_ref_prior = {"high": max(b[1] for b in pbe),
                                    "low": min(b[2] for b in pbe)}
                else:
                    es_ref_prior = {"high": None, "low": None}
            if n_win == 2 and (i == 0 or ref_prior["high"] is None):
                continue
            ctx = {
                "session": s, "n_win": n_win,
                "m5": nq[s][0], "b5": nq[s][1],
                "es": es.get(s), "feats": feats[s], "cd": cds[s],
                "med60": med60[s], "q_ls": q_ls[s],
                "ref_prior": ref_prior, "es_ref_prior": es_ref_prior,
            }
            all_events.extend(scan_session(ctx))

    months = sorted({s[:7] for s in sessions})
    out: dict = {"meta": {
        "run_date": "2026-08-20", "spec": "SDD v1.0 Phase 1",
        "sessions": [sessions[0], sessions[-1], len(sessions)],
        "es_sessions": len(es),
        "params": {"W": W, "disp_mult": DISP_MULT, "df_min_gap": DF_MIN_GAP,
                   "ls_trail": LS_TRAIL_SESS, "headline_h": HEADLINE_H,
                   "horizons": list(HORIZONS), "n_sweep": list(N_SWEEP)},
    }}

    for n_win in N_SWEEP:
        evs = [e for e in all_events if e["n_win"] == n_win]
        key = f"N{n_win}"
        trig = [e for e in evs if e["disp_j"] is not None]

        def fomc_ok(e: dict) -> bool:
            return e["session"] not in fomc or e["disp_minute"] <= FOMC_CUTOFF

        # cells (FOMC applied to trade cells)
        base = [e for e in trig if e["smt"] is True and fomc_ok(e)]
        nonsmt = [e for e in trig if e["smt"] is False and fomc_ok(e)]
        cells = {
            "BASE": base,
            "BASE+thin": [e for e in base if e["thin"] is True],
            "BASE+delta_fail": [e for e in base if e["delta_fail"] is True],
            "BASE+disp_q": [e for e in base if e["disp_delta_q"] is True],
            "COMBO_A": [e for e in base if e["thin"] is True and e["delta_fail"] is True],
        }
        cells["COMBO_B"] = [e for e in cells["COMBO_A"] if e["disp_delta_q"] is True]

        # 1. funnel, first
        smt_evs = [e for e in evs if e["smt"] is True]
        funnel = {
            "extremes": len(evs),
            "es_alignable": sum(1 for e in evs if e["smt"] is not None),
            "smt": len(smt_evs),
            "smt_x_thin": sum(1 for e in smt_evs if e["thin"] is True),
            "smt_x_thin_x_delta_fail": sum(
                1 for e in smt_evs if e["thin"] is True and e["delta_fail"] is True),
            "smt_x_thin_x_df_x_disp": sum(
                1 for e in smt_evs if e["thin"] is True and e["delta_fail"] is True
                and e["disp_j"] is not None),
            "per_month_median": {c: monthly_median(months, [e["session"] for e in rows])
                                 for c, rows in cells.items()},
            "journal": {
                "superseded_prints": sum(1 for e in evs if e["superseded"]),
                "es_missing": sum(1 for e in evs if e["smt"] is None),
                "thin_undefined": sum(1 for e in evs if e["thin"] is None),
                "delta_fail_undefined": sum(1 for e in evs if e["delta_fail"] is None),
                "fomc_excluded": sum(1 for e in trig if e["smt"] is True and not fomc_ok(e)),
                "sessions_over_cap2": sum(
                    1 for s in sessions
                    if sum(1 for e in base if e["session"] == s) > 2),
            },
        }

        # 2. component matrix
        matrix = {}
        for ci, (c, rows) in enumerate(cells.items()):
            matrix[c] = fwd_panel(rows)
            if c != "BASE":
                matrix[c]["vs_base"] = vs_base(rows, base, args.draws, 20260820 + ci)
        matrix["COMBO_B"]["vs_combo_a"] = vs_base(
            cells["COMBO_B"], cells["COMBO_A"], args.draws, 20260899)
        matrix["disp_ls_quartile_gradient"] = {
            str(q): fwd_panel([e for e in base if e.get("disp_ls_quartile") == q])
            for q in (1, 2, 3, 4)}

        # 3. SMT-alone panels (fade direction from the extreme bar close)
        def ext_fade_rows(rows: list[dict]) -> list[dict]:
            out_r = []
            for e in rows:
                m5, b5 = nq[e["session"]]
                j = e["ext_j"]
                if j + max(HORIZONS) >= len(m5):
                    continue
                closes = [b[3] for b in b5]
                dsign = -1 if e["side"] == "high" else 1
                out_r.append({"fwd": {h: dsign * (closes[j + h] - closes[j])
                                      for h in HORIZONS}})
            return out_r
        smt_alone = {
            "smt_extremes_fade": fwd_panel(ext_fade_rows(smt_evs)),
            "confirmed_extremes_fade": fwd_panel(
                ext_fade_rows([e for e in evs if e["smt"] is False])),
        }

        # 4. controls
        rng = random.Random(20260820)
        # day-shuffle placement on BASE
        perm_means = []
        obs = statistics.mean(e["fwd"][HEADLINE_H] for e in base) if base else 0.0
        for _ in range(100):
            vals = []
            for e in base:
                s = rng.choice(sessions)
                m5, b5 = nq[s]
                j = bisect_left(m5, e["disp_minute"])
                if j < len(m5) and m5[j] == e["disp_minute"] and j + HEADLINE_H < len(m5):
                    vals.append(e["dir"] * (b5[j + HEADLINE_H][3] - b5[j][3]))
            perm_means.append(statistics.mean(vals) if vals else 0.0)
        controls = {"day_shuffle_BASE": {
            "observed_fwd6_pts": round(obs, 2),
            "perm_mean": round(statistics.mean(perm_means), 2),
            "perm_sd": round(statistics.stdev(perm_means), 2),
            "p_value": round(sum(p >= obs for p in perm_means) / 100, 3),
        }}

        # thin-scramble: resample the extreme bar's (large_share, f1_5) from
        # random other bars of the same session, reclassify #1, delta vs BASE
        def cell_delta(rows: list[dict]) -> float | None:
            if len(rows) < 3 or not base:
                return None
            return (statistics.mean(e["fwd"][HEADLINE_H] for e in rows)
                    - statistics.mean(e["fwd"][HEADLINE_H] for e in base))
        obs_d_thin = cell_delta(cells["BASE+thin"])
        perm_d = []
        for _ in range(args.permutations):
            rows = []
            for e in base:
                f = feats[e["session"]]
                recs = [b for b in f["bars"].values()
                        if isinstance(b, dict) and b.get("large_share") is not None
                        and b.get("f1_5") is not None]
                r = rng.choice(recs)
                m60 = med60[e["session"]]
                if m60 is None:
                    continue
                if r["large_share"] <= m60 and abs(r["f1_5"]) < f["q70"]:
                    rows.append(e)
            d = cell_delta(rows)
            perm_d.append(d if d is not None else 0.0)
        controls["thin_scramble"] = {
            "observed_delta_pts": round(obs_d_thin, 2) if obs_d_thin is not None else None,
            "perm_mean": round(statistics.mean(perm_d), 2),
            "perm_sd": round(statistics.stdev(perm_d), 2),
            "p_value": (round(sum(p >= obs_d_thin for p in perm_d) / len(perm_d), 3)
                        if obs_d_thin is not None else None),
        } if perm_d else {"n": 0}

        # CD-scramble: permute per-minute deltas within session, rebuild CD,
        # reclassify #2, delta vs BASE
        obs_d_df = cell_delta(cells["BASE+delta_fail"])
        sess_deltas: dict[str, list] = {}
        for e in base:
            s = e["session"]
            if s not in sess_deltas and cds[s] is not None:
                mins = json.loads((FLOW / f"{s}.json").read_text())["minutes"]
                sess_deltas[s] = [mins.get(str(k), {}).get("buy", 0)
                                  - mins.get(str(k), {}).get("sell", 0)
                                  for k in range(1, 391)]
        perm_d = []
        for _ in range(args.permutations):
            rows = []
            cd_perm: dict[str, list] = {}
            for s, deltas in sess_deltas.items():
                dd = deltas[:]
                rng.shuffle(dd)
                pref = list(accumulate(dd))
                cd_perm[s] = [pref[min(m - 570, 390) - 1] for m in nq[s][0]]
            for e in base:
                cd = cd_perm.get(e["session"])
                if cd is None or e["delta_fail"] is None:
                    continue
                # recompute against the same prior-extreme geometry
                m5 = nq[e["session"]][0]
                j = e["ext_j"]
                p = e.get("df_prior_j")
                if p is None:
                    continue
                df = cd[j] < cd[p] if e["side"] == "high" else cd[j] > cd[p]
                if df:
                    rows.append(e)
            d = cell_delta(rows)
            perm_d.append(d if d is not None else 0.0)
        controls["cd_scramble"] = {
            "observed_delta_pts": round(obs_d_df, 2) if obs_d_df is not None else None,
            "perm_mean": round(statistics.mean(perm_d), 2) if perm_d else None,
            "perm_sd": round(statistics.stdev(perm_d), 2) if len(perm_d) > 2 else None,
            "p_value": (round(sum(p >= obs_d_df for p in perm_d) / len(perm_d), 3)
                        if obs_d_df is not None and perm_d else None),
        }

        # location control: SMT vs confirmed extremes, same trigger
        if base and nonsmt:
            bv = [e["fwd"][HEADLINE_H] for e in base]
            nv = [e["fwd"][HEADLINE_H] for e in nonsmt]
            obs_diff = statistics.mean(bv) - statistics.mean(nv)
            pool = bv + nv
            ge = 0
            for _ in range(1000):
                rng.shuffle(pool)
                if statistics.mean(pool[:len(bv)]) - statistics.mean(pool[len(bv):]) >= obs_diff:
                    ge += 1
            controls["location_smt_vs_confirmed"] = {
                "smt_fwd6": round(statistics.mean(bv), 2), "n_smt": len(bv),
                "confirmed_fwd6": round(statistics.mean(nv), 2), "n_confirmed": len(nv),
                "diff_pts": round(obs_diff, 2), "p_one_sided": round(ge / 1000, 3),
            }

        # 5. gates
        gates = {}
        for c, rows in cells.items():
            p = matrix[c]
            pos_t = [h for h in HORIZONS
                     if p.get(f"h{h}", {}).get("mean_pts", 0) > 0
                     and p.get(f"h{h}", {}).get("t", 0) >= GATE_T]
            g = {"n": p["n"], "n_ok": p["n"] >= GATE_N,
                 "horizons_clearing": pos_t, "t_ok": len(pos_t) >= 2,
                 "freq_ok": funnel["per_month_median"][c] >= GATE_FREQ}
            if c != "BASE":
                adds = False
                for hk in (f"h{HEADLINE_H}", "h13"):
                    v = matrix[c]["vs_base"].get(hk)
                    if v and (v["d_ev_usd"] >= MARGIN_EV or v["d_hit"] >= MARGIN_HIT) \
                            and min(v["p_ev"], v["p_hit"]) < 0.10:
                        adds = True
                g["adds_vs_base"] = adds
            gates[c] = g
        gates["proceed"] = any(
            g["n_ok"] and g["t_ok"] and g["freq_ok"]
            for c, g in gates.items() if isinstance(g, dict))

        out[key] = {"funnel": funnel, "matrix": matrix, "smt_alone": smt_alone,
                    "controls": controls, "gates": gates}

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "phase1.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    with (args.out / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in all_events:
            fh.write(json.dumps(e) + "\n")
    for key in ("N2", "N1"):
        print(f"\n===== {key} =====")
        print(json.dumps(out[key]["funnel"], indent=1))
        print(json.dumps({c: {k: v for k, v in p.items() if k != "vs_base"}
                          for c, p in out[key]["matrix"].items()
                          if c != "disp_ls_quartile_gradient"}, indent=1))
        print(json.dumps(out[key]["gates"], indent=1))
    print(f"\nwritten -> {args.out}")


if __name__ == "__main__":
    main()
