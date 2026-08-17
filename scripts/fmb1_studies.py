"""FMB-1: four $0 Phase-1 window-phenomenon studies (RCP, CST, ODC, PAID).

Overlay-first doctrine (FRB-1 inherited): every study is descriptive
measurement against fixtures already on disk. Nothing trades; gates are
pre-registered in the batch spec; each subcommand prints its panel and
writes a JSON summary for the per-program report.

Common geometry (all four): 1 NQ, one shot per session, fixed bracket
nominal 100-pt target / 50-pt stop AT SPEC-LOCK (2026-08-17, NQ ref
30250), applied historically as price FRACTIONS (target 0.3306%, stop
0.1653%) so 2020's bracket is geometrically identical -- NAIM's
fixed-point erosion is not repeated. Costs: $10/RT + 1 tick adverse on
the market entry and 1 further tick adverse on stop-market exits;
targets are limits filled at touch. Bar-level path ambiguity resolves
ADVERSE-FIRST (stop checked before target inside every bar, both
directions) -- conservative by construction.

All conditioning series are walk-forward: thresholds consulted at time t
derive from data strictly before t.

Usage:
  uv run python scripts/fmb1_studies.py rcp   [--out var/fmb1]
  uv run python scripts/fmb1_studies.py cst   [--out var/fmb1]
  uv run python scripts/fmb1_studies.py odc   [--out var/fmb1]
  uv run python scripts/fmb1_studies.py paid  [--out var/fmb1]
  uv run python scripts/fmb1_studies.py overlap [--out var/fmb1]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FIX_1M = Path("var/fixtures/1m")          # NQ pseudo-tick, 00:00-16:31 ET, 2020->
FIX_ZN = Path("var/fixtures/zn-1m")       # ZN pseudo-tick, RTH (pulled for CST)
FIX_TRADES = Path("var/fixtures/trades")  # NQ ticks w/ aggressor side, tick year
FOMC = Path("config/fomc_dates.json")

POINT_VALUE = 20.0
RT_COST = 10.0
TICK = 0.25
P_REF = 30250.0                            # NQ at spec-lock 2026-08-17
TGT_F = 100.0 / P_REF
STP_F = 50.0 / P_REF


# ---------------------------------------------------------------- loaders


def sessions_on_disk(root: Path = FIX_1M) -> list[str]:
    return sorted(p.stem for p in root.glob("*.jsonl"))


def load_bars(session: str, root: Path = FIX_1M) -> tuple[list[int], list[tuple]]:
    """Parse a pseudo-tick fixture into per-minute (o, h, l, c) bars.

    The writer emits exactly four prints per minute in O/H/L/C order, so
    bars are rebuilt by grouping on the minute of the timestamp. Minutes
    are ET minutes-from-midnight; the UTC offset is taken once per file
    (fixtures never straddle a DST change intraday).
    """
    lines = (root / f"{session}.jsonl").read_text().splitlines()
    if not lines:
        return [], []
    first_ts = lines[0].split('"')[3]
    utc = datetime.fromisoformat(first_ts)
    offset = int(utc.astimezone(ET).utcoffset().total_seconds() // 60) if False else int(
        (utc.astimezone(ET).replace(tzinfo=None) - utc.replace(tzinfo=None)).total_seconds() // 60
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


def price_at(minutes: list[int], bars: list[tuple], minute: int, field: int = 0) -> float | None:
    """Open (field 0) of the bar AT `minute`, or None if that minute is absent."""
    i = bisect_left(minutes, minute)
    if i < len(minutes) and minutes[i] == minute:
        return bars[i][field]
    return None


def close_at_or_before(minutes: list[int], bars: list[tuple], minute: int) -> float | None:
    i = bisect_left(minutes, minute + 1) - 1
    return bars[i][3] if i >= 0 else None


# ---------------------------------------------------------------- bracket


def first_passage(
    minutes: list[int],
    bars: list[tuple],
    entry_minute: int,
    direction: int,
    deadline_minute: int,
) -> dict | None:
    """One-shot fixed-fraction bracket from entry_minute's open.

    Entry: market at bar open + 1 tick adverse. Stop: stop-market at
    entry*(1 -/+ STP_F), filled 1 further tick adverse. Target: limit at
    entry*(1 +/- TGT_F), filled at touch. Adverse-first inside each bar.
    Deadline: exit at the open of the first bar >= deadline_minute (or
    the session's last close if the session ends early).
    """
    i = bisect_left(minutes, entry_minute)
    if i >= len(minutes) or minutes[i] != entry_minute:
        return None
    entry = bars[i][0] + direction * TICK
    tgt = entry * (1 + direction * TGT_F)
    stp = entry * (1 - direction * STP_F)
    for j in range(i, len(minutes)):
        m = minutes[j]
        o, h, l, c = bars[j]
        if m >= deadline_minute:
            exit_px, outcome, exit_min = o, "time", m
            break
        if direction == 1:
            if l <= stp:
                exit_px, outcome, exit_min = stp - TICK, "stop", m
                break
            if h >= tgt:
                exit_px, outcome, exit_min = tgt, "target", m
                break
        else:
            if h >= stp:
                exit_px, outcome, exit_min = stp + TICK, "stop", m
                break
            if l <= tgt:
                exit_px, outcome, exit_min = tgt, "target", m
                break
    else:
        exit_px, outcome, exit_min = bars[-1][3], "time", minutes[-1]
    pnl = direction * (exit_px - entry) * POINT_VALUE - RT_COST
    return {
        "outcome": outcome,
        "pnl": round(pnl, 2),
        "entry": entry,
        "exit": exit_px,
        "minutes_held": exit_min - entry_minute,
    }


def bracket_panel(results: list[dict]) -> dict:
    n = len(results)
    if not n:
        return {"n": 0}
    hits = sum(r["outcome"] == "target" for r in results)
    stops = sum(r["outcome"] == "stop" for r in results)
    times = n - hits - stops
    pnls = [r["pnl"] for r in results]
    return {
        "n": n,
        "hit_rate": round(hits / n, 4),
        "stop_rate": round(stops / n, 4),
        "time_rate": round(times / n, 4),
        "ev": round(sum(pnls) / n, 2),
        "total": round(sum(pnls), 2),
        "t": round(t_stat(pnls), 2),
        "time_exit_ev": round(
            sum(r["pnl"] for r in results if r["outcome"] == "time") / times, 2
        ) if times else None,
    }


# ---------------------------------------------------------------- stats


def t_stat(values: list[float]) -> float:
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    sd = statistics.stdev(values)
    return mean / (sd / math.sqrt(n)) if sd else 0.0


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 6),
        "median": round(statistics.median(values), 6),
        "share_pos": round(sum(v > 0 for v in values) / len(values), 3),
        "t": round(t_stat(values), 2),
    }


# ---------------------------------------------------------------- RCP


def quarterly_expiries(first: str, last: str) -> list[str]:
    """Third Friday of Mar/Jun/Sep/Dec between the fixture bounds."""
    out = []
    for year in range(int(first[:4]), int(last[:4]) + 1):
        for month in (3, 6, 9, 12):
            d = date(year, month, 15)
            d += timedelta(days=(4 - d.weekday()) % 7)
            iso = d.isoformat()
            if first <= iso <= last:
                out.append(iso)
    return out


def study_rcp() -> dict:
    sessions = sessions_on_disk()
    expiries = quarterly_expiries(sessions[0], sessions[-1])
    # offset day T-k = k-th session strictly before the expiry date
    roll_days: list[dict] = []  # {expiry, k, session}
    for expiry in expiries:
        idx = bisect_left(sessions, expiry)
        for k in range(1, 9):
            if idx - k >= 0:
                roll_days.append({"expiry": expiry, "k": k, "session": sessions[idx - k]})

    # single pass over the ~200 involved sessions
    by_session: dict[str, list[dict]] = defaultdict(list)
    for rd in roll_days:
        by_session[rd["session"]].append(rd)
    returns: dict[int, list[tuple[str, float]]] = defaultdict(list)  # k -> [(expiry, ret_frac)]
    cached: dict[str, tuple] = {}
    for session, rds in sorted(by_session.items()):
        minutes, bars = load_bars(session)
        cached[session] = (minutes, bars)
        o = price_at(minutes, bars, 570)
        c = close_at_or_before(minutes, bars, 960)
        if o is None or c is None:
            continue
        for rd in rds:
            returns[rd["k"]].append((rd["expiry"], (c - o) / o))

    signature = {}
    for k in range(1, 9):
        vals = [r for _, r in returns[k]]
        pos = sum(v > 0 for v in vals)
        maj = max(pos, len(vals) - pos)
        signature[f"T-{k}"] = {
            **summarize(vals),
            "sign_consistency": round(maj / len(vals), 3) if vals else None,
            "majority": "long" if pos * 2 >= len(vals) else "short",
        }

    # walk-forward bracket simulation: direction per offset from PRIOR rolls only
    trades: list[dict] = []
    for k in range(1, 9):
        series = returns[k]  # chronological by expiry
        for i, (expiry, _ret) in enumerate(series):
            prior = [r for _, r in series[:i]]
            if len(prior) < 12:
                continue
            pos = sum(v > 0 for v in prior)
            consistency = max(pos, len(prior) - pos) / len(prior)
            if consistency < 0.70:
                continue
            direction = 1 if pos * 2 >= len(prior) else -1
            session = next(rd["session"] for rd in roll_days
                           if rd["expiry"] == expiry and rd["k"] == k)
            minutes, bars = cached[session]
            result = first_passage(minutes, bars, 600, direction, 955)
            if result:
                trades.append({**result, "session": session, "k": k, "direction": direction})

    stable = [k for k in range(1, 9)
              if (signature[f"T-{k}"].get("n", 0) >= 20
                  and (signature[f"T-{k}"]["sign_consistency"] or 0) >= 0.70)]
    panel = bracket_panel(trades)
    return {
        "study": "rcp",
        "n_expiries": len(expiries),
        "signature": signature,
        "stable_offsets": stable,
        "walk_forward": panel,
        "walk_forward_by_k": {k: bracket_panel([t for t in trades if t["k"] == k])
                              for k in sorted({t["k"] for t in trades})},
        "trade_days": sorted({t["session"] for t in trades}),
        "volume_migration": "NOT MEASURABLE AT $0: fixtures are the continuous "
                            "build without per-contract volumes; recorded as a "
                            "spec assumption that does not hold on disk",
        "gate": {"stable_offsets_needed": 3, "stable_offsets_found": len(stable),
                 "hit_rate": panel.get("hit_rate"), "n": panel.get("n")},
    }


# ---------------------------------------------------------------- ODC


def study_odc(close_imbalance: dict[str, float] | None = None) -> dict:
    sessions = sessions_on_disk()
    rows: list[dict] = []
    prev_close: float | None = None
    trades_all: list[dict] = []
    for session in sessions:
        minutes, bars = load_bars(session)
        c_0230 = close_at_or_before(minutes, bars, 150)
        c_0830 = close_at_or_before(minutes, bars, 510)
        o_0000 = bars[0][0] if minutes and minutes[0] <= 5 else None
        o_0300 = price_at(minutes, bars, 180)
        i_lo = bisect_left(minutes, 150)
        i_hi = bisect_left(minutes, 511)
        window = bars[i_lo:i_hi]
        this_close = close_at_or_before(minutes, bars, 960)
        row: dict = {"session": session, "dow": date.fromisoformat(session).weekday()}
        if c_0230 and c_0830 and o_0300 and o_0000 and prev_close and len(window) > 300:
            row["window_ret"] = (c_0830 - c_0230) / c_0230
            row["range_frac"] = (max(b[1] for b in window) - min(b[2] for b in window)) / c_0230
            row["asia"] = 1 if o_0000 > prev_close else -1
            row["europe"] = 1 if o_0300 > o_0000 else -1
            row["align"] = (row["asia"] > 0) + (row["europe"] > 0)
            result = first_passage(minutes, bars, 180, 1, 510)
            if result:
                row["trade"] = result
                trades_all.append({**result, "session": session, "align": row["align"],
                                   "dow": row["dow"]})
            rows.append(row)
        prev_close = this_close or prev_close

    usable = [r for r in rows if "window_ret" in r]
    by_year: dict[str, list[float]] = defaultdict(list)
    for r in usable:
        by_year[r["session"][:4]].append(r["window_ret"])
    by_dow = {d: summarize([r["window_ret"] for r in usable if r["dow"] == d])
              for d in range(5)}
    by_align = {a: {
        "window_ret": summarize([r["window_ret"] for r in usable if r["align"] == a]),
        "bracket": bracket_panel([t for t in trades_all if t["align"] == a]),
    } for a in (0, 1, 2)}

    # tick-year refinement: prior-close inventory condition (net selling into
    # the close -> dealers long inventory -> paid to hold -> overnight up)
    inventory_panel = None
    if close_imbalance:
        imb_sessions = sorted(close_imbalance)
        tagged = []
        for t in trades_all:
            i = bisect_left(imb_sessions, t["session"])
            if i > 0 and imb_sessions[i - 1] >= "2025-08-13":
                prior = imb_sessions[i - 1]
                tagged.append({**t, "inv_sell": close_imbalance[prior] < 0})
        inventory_panel = {
            "prior_net_sell": bracket_panel([t for t in tagged if t["inv_sell"]]),
            "prior_net_buy": bracket_panel([t for t in tagged if not t["inv_sell"]]),
            "cond3_sell_and_align2": bracket_panel(
                [t for t in tagged if t["inv_sell"] and t["align"] == 2]),
        }

    med_range = statistics.median([r["range_frac"] for r in usable])
    year_summary = {y: summarize(v) for y, v in sorted(by_year.items())}
    conditioned = bracket_panel([t for t in trades_all if t["align"] == 2])
    return {
        "study": "odc",
        "n_sessions": len(usable),
        "window_ret_all": summarize([r["window_ret"] for r in usable]),
        "by_year": year_summary,
        "by_dow": by_dow,
        "by_align": by_align,
        "unconditional_bracket": bracket_panel(trades_all),
        "conditioned_bracket": conditioned,
        "inventory_tick_year": inventory_panel,
        "magnitude": {
            "median_window_range_frac": round(med_range, 6),
            "target_frac": round(TGT_F, 6),
            "sixty_pt_equiv_frac": round(60 / P_REF, 6),
            "range_covers_target": med_range >= TGT_F,
        },
        "trade_days": sorted({t["session"] for t in trades_all if t["align"] == 2}),
        "gate": {"conditioned_hit": conditioned.get("hit_rate"),
                 "n": conditioned.get("n"),
                 "recent4_years_positive": sum(
                     1 for y in ("2022", "2023", "2024", "2025")
                     if year_summary.get(y, {"mean": 0}).get("mean", 0) > 0)},
    }


# ---------------------------------------------------------------- PAID


def utc_bounds(session: str, h: int, m: int, h2: int, m2: int) -> tuple[str, str]:
    d = date.fromisoformat(session)
    start = datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
    end = datetime(d.year, d.month, d.day, h2, m2, tzinfo=ET)
    fmt = "%Y-%m-%dT%H:%M"
    return (start.astimezone(ZoneInfo("UTC")).strftime(fmt),
            end.astimezone(ZoneInfo("UTC")).strftime(fmt))


def close_flow_series() -> dict[str, float]:
    """Signed aggressor imbalance over 15:50-16:00 ET per tick-year session.

    'B' = buy aggressor, 'A' = sell aggressor (Databento convention, same
    mapping as nq_agent.flow). 'N'/missing sides are skipped -- declared;
    the tick year's median tick-rule agreement is 84.3% (TFR.md) and the
    imbalance is a ratio, so dropped ticks bias magnitude, not sign.
    """
    out: dict[str, float] = {}
    for path in sorted(FIX_TRADES.glob("*.jsonl")):
        session = path.stem
        lo, hi = utc_bounds(session, 15, 50, 16, 0)
        raw = path.read_text()
        pos = raw.find(lo[:16])
        while pos == -1 and lo < hi:  # no tick in the exact first minute
            lo_dt = datetime.strptime(lo, "%Y-%m-%dT%H:%M") + timedelta(minutes=1)
            lo = lo_dt.strftime("%Y-%m-%dT%H:%M")
            pos = raw.find(lo[:16])
        if pos == -1:
            continue
        start = raw.rfind("\n", 0, pos) + 1
        buy = sell = 0
        for line in raw[start:].splitlines():
            parts = line.split('"')
            ts = parts[3]
            if ts[:16] >= hi:
                break
            if len(parts) < 14:
                continue
            size = int(parts[10].strip(" :,"))
            side = parts[13]
            if side == "B":
                buy += size
            elif side == "A":
                sell += size
        if buy + sell:
            out[session] = (buy - sell) / (buy + sell)
    return out


def study_paid(imb: dict[str, float]) -> dict:
    sessions = sessions_on_disk()
    imb_days = sorted(imb)
    qualifying: list[dict] = []
    for i, day in enumerate(imb_days):
        prior = [abs(imb[d]) for d in imb_days[:i]][-60:]
        if len(prior) < 30:
            continue
        q80 = statistics.quantiles(prior, n=5)[-1]
        if abs(imb[day]) < q80:
            continue
        j = bisect_left(sessions, day)
        if j + 1 >= len(sessions) or sessions[j] != day:
            continue
        nxt = sessions[j + 1]
        d = date.fromisoformat(day)
        qualifying.append({
            "imb_day": day, "trade_day": nxt, "imb": imb[day],
            "direction": -1 if imb[day] > 0 else 1,
            "rebalance": (d.month != (d + timedelta(days=3)).month),
        })

    trades, fwd = [], defaultdict(list)
    for q in qualifying:
        minutes, bars = load_bars(q["trade_day"])
        entry_o = price_at(minutes, bars, 575)
        if entry_o is None:
            continue
        for h in (15, 30, 60, 120):
            c = close_at_or_before(minutes, bars, 575 + h)
            if c:
                fwd[h].append(q["direction"] * (c - entry_o) / entry_o)
        result = first_passage(minutes, bars, 575, q["direction"], 695)
        if result:
            trades.append({**result, **q})

    panel = bracket_panel(trades)
    horizons = {f"{h}m": summarize(v) for h, v in fwd.items()}
    sig = sum(1 for h in fwd.values() if t_stat(h) >= 2.0)
    return {
        "study": "paid",
        "n_close_flow_sessions": len(imb),
        "imbalance_series": summarize(list(imb.values())),
        "n_qualifying": len(qualifying),
        "unwind_forward_returns": horizons,
        "horizons_with_t_ge_2": sig,
        "bracket": panel,
        "bracket_rebalance_days": bracket_panel([t for t in trades if t["rebalance"]]),
        "bracket_normal_days": bracket_panel([t for t in trades if not t["rebalance"]]),
        "trade_days": sorted({t["trade_day"] for t in trades}),
        "gate": {"horizons_t_ge_2_needed": 2, "found": sig,
                 "hit_rate": panel.get("hit_rate"), "n": panel.get("n")},
    }


# ---------------------------------------------------------------- CST


def study_cst(k_sigma: float = 2.5) -> dict:
    if not FIX_ZN.exists() or not any(FIX_ZN.glob("*.jsonl")):
        return {"study": "cst", "blocked": "no ZN fixtures on disk"}
    fomc = set(json.loads(FOMC.read_text())["dates"])
    zn_sessions = sessions_on_disk(FIX_ZN)
    nq_sessions = set(sessions_on_disk())

    closes_by_session: dict[str, dict[int, float]] = {}
    for session in zn_sessions:
        minutes, bars = load_bars(session, FIX_ZN)
        closes_by_session[session] = dict(zip(minutes, (b[3] for b in bars)))

    history: dict[int, list[float]] = defaultdict(list)  # minute -> trailing r5
    shocks: list[dict] = []
    shock_counts = {2.0: 0, 2.5: 0, 3.0: 0}
    for session in zn_sessions:
        closes = closes_by_session[session]
        session_shock = None
        for m in range(575, 956):
            if m not in closes or m - 5 not in closes:
                continue
            r5 = closes[m] - closes[m - 5]
            trail = history[m]
            if len(trail) >= 30:
                sigma = statistics.stdev(trail[-60:])
                if sigma:
                    for kk in shock_counts:
                        if abs(r5) >= kk * sigma:
                            shock_counts[kk] += 1
                    if abs(r5) >= k_sigma * sigma and session_shock is None:
                        scheduled = (600 <= m <= 602) or (
                            session in fomc and 835 <= m <= 870)
                        if not (session in fomc and 835 <= m <= 870):
                            session_shock = {
                                "session": session, "minute": m,
                                "direction": 1 if r5 > 0 else -1,  # notes up -> NQ long
                                "magnitude": abs(r5) / sigma,
                                "scheduled": scheduled,
                            }
        for m in range(575, 956):
            if m in closes and m - 5 in closes:
                history[m].append(closes[m] - closes[m - 5])
                if len(history[m]) > 60:
                    history[m] = history[m][-60:]
        if session_shock and session in nq_sessions:
            shocks.append(session_shock)

    trades, fwd = [], defaultdict(list)
    for s in shocks:
        minutes, bars = load_bars(s["session"])
        entry_minute = s["minute"] + 1
        entry_o = price_at(minutes, bars, entry_minute)
        if entry_o is None:
            continue
        for h in (5, 15, 30, 60, 90):
            c = close_at_or_before(minutes, bars, entry_minute + h)
            if c:
                fwd[h].append((s["direction"] * (c - entry_o) / entry_o, s["scheduled"]))
        deadline = min(entry_minute + 90, 955)
        result = first_passage(minutes, bars, entry_minute, s["direction"], deadline)
        if result:
            trades.append({**result, **s})

    unsched = {f"{h}m": summarize([r for r, sch in v if not sch]) for h, v in fwd.items()}
    sig = sum(1 for h in fwd.values()
              if t_stat([r for r, sch in h if not sch]) >= 2.0
              and sum(r for r, sch in h if not sch) > 0)
    panel = bracket_panel(trades)
    return {
        "study": "cst",
        "k_sigma": k_sigma,
        "n_zn_sessions": len(zn_sessions),
        "shock_days_by_k": shock_counts,
        "n_shock_days": len(shocks),
        "n_unscheduled": sum(not s["scheduled"] for s in shocks),
        "transmitted_forward_unscheduled": unsched,
        "horizons_positive_t_ge_2": sig,
        "bracket": panel,
        "bracket_unscheduled": bracket_panel([t for t in trades if not t["scheduled"]]),
        "trade_days": sorted({t["session"] for t in trades}),
        "gate": {"horizons_needed": 2, "found": sig, "n_unscheduled_min": 100,
                 "hit_rate": panel.get("hit_rate")},
    }


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", choices=["rcp", "cst", "odc", "paid", "overlap"])
    parser.add_argument("--out", type=Path, default=Path("var/fmb1"))
    parser.add_argument("--k-sigma", type=float, default=2.5)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    if args.study == "rcp":
        result = study_rcp()
    elif args.study == "cst":
        result = study_cst(args.k_sigma)
    elif args.study == "odc":
        imb_path = args.out / "paid_close_flow.json"
        imb = json.loads(imb_path.read_text()) if imb_path.exists() else None
        result = study_odc(imb)
    elif args.study == "paid":
        imb_path = args.out / "paid_close_flow.json"
        if imb_path.exists():
            imb = json.loads(imb_path.read_text())
        else:
            imb = close_flow_series()
            imb_path.write_text(json.dumps(imb))
        result = study_paid(imb)
    else:  # overlap
        days = {}
        for name in ("rcp", "cst", "odc", "paid"):
            p = args.out / f"{name}.json"
            if p.exists():
                days[name] = set(json.loads(p.read_text()).get("trade_days", []))
        names = sorted(days)
        result = {
            "study": "overlap",
            "trade_day_counts": {n: len(days[n]) for n in names},
            "pairwise": {f"{a}&{b}": len(days[a] & days[b])
                         for i, a in enumerate(names) for b in names[i + 1:]},
        }

    (args.out / f"{args.study}.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
