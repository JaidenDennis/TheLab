"""SSX-V3 Phase 1: small-target / wide-stop first passage on fc_t13 entries.

The free measurement (spec SSX-V3 v1.0 section 3): a column-join on the
fc_t13 develop journal plus any shadow trades with fixture coverage,
walked forward through the 1m pseudo-tick fixtures. For every historical
entry (price already includes the 1-tick adverse fill), does price touch
+20 points (fraction form, spec-lock NQ 30250) before the house 0.35%
catastrophic stop? Zero lookahead, zero new entries, zero strategy code.

House conventions (FMB-1 inherited): targets are limits filled at touch;
stops are stop-markets filled 1 further tick adverse; bar-level path
ambiguity resolves ADVERSE-FIRST (stop checked before target inside every
bar); $10/RT. Deadline 15:55 ET flatten at that bar's close -- the same
bar the engine's tracker tests before the strategy flattens. The
companion cell adds the 13-bar (65 min) time backstop.

Deliverables, all pre-declared: unconditional first-passage rates and EV;
time-to-hit distribution; the touch-rate ladder {10,15,20,25,30,40}
(recorded, not used to move the target); stop-side anatomy (monthly
counts, worst 5-session window, share of total loss); and one-at-a-time
conditional cuts (|F1_5| percentile bucket, entry hour, gamma day-type).
The decision files carry percentile keys only up to Q85, so the top
bucket is >=Q85 rather than the spec's Q90+ -- recorded, not silently
substituted.

Usage:
  uv run python scripts/ssx_v3_phase1.py [--out var/ssx]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FIX_1M = Path("var/fixtures/1m")
JOURNALS = [
    ("develop", Path("var/tfr/fc_t13/journal")),
    ("shadow", Path("var/shadow/journal")),
]
DECISIONS = Path("var/decisions/k3m")
GAMMA = Path("var/gamma/regimes.jsonl")

POINT_VALUE = 20.0
RT_COST = 10.0
TICK = 0.25
P_REF = 30250.0                      # NQ at spec-lock 2026-08-17
STP_F = 0.0035                       # the house catastrophic stop
LADDER_PTS = [10, 15, 20, 25, 30, 40]
FLATTEN_MIN = 15 * 60 + 54           # last 1m bar tested; its close = the 15:55 flatten
T13_MINUTES = 65                     # 13 five-minute bars


# ---------------------------------------------------------------- loaders


def load_bars(session: str) -> tuple[list[int], list[tuple]]:
    """Per-minute (o, h, l, c) bars from the 4-print pseudo-tick fixture."""
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


def load_journal_trades() -> tuple[list[dict], int]:
    """position_closed records with fixture coverage; (trades, n_excluded)."""
    trades: list[dict] = []
    excluded = 0
    for source, journal_dir in JOURNALS:
        for path in sorted(journal_dir.glob("*.jsonl")):
            for line in path.read_text().splitlines():
                if '"position_closed"' not in line:
                    continue
                record = json.loads(line)
                if record.get("event") != "position_closed":
                    continue
                entry_et = datetime.fromisoformat(record["entry_time"]).astimezone(ET)
                session = entry_et.date().isoformat()
                if not (FIX_1M / f"{session}.jsonl").exists():
                    excluded += 1  # shadow trades past fixture coverage
                    continue
                trades.append(
                    {
                        "source": source,
                        "session": session,
                        "direction": 1 if record["direction"] == "LONG" else -1,
                        "entry": float(record["entry_price"]),
                        "entry_minute": entry_et.hour * 60 + entry_et.minute,
                        "entry_hour": entry_et.hour,
                        "fc_t13_pnl": float(record["realised_pnl"]),
                    }
                )
    trades.sort(key=lambda t: (t["session"], t["entry_minute"]))
    return trades, excluded


def load_gamma() -> dict[str, str]:
    if not GAMMA.exists():
        return {}
    out = {}
    for line in GAMMA.read_text().splitlines():
        record = json.loads(line)
        out[record["date"]] = record["regime"]
    return out


def decision_record(cache: dict, session: str, entry_minute: int) -> tuple[dict, dict] | None:
    """(bar record at the entry's 5m index, day q_f1 table) or None."""
    if session not in cache:
        path = DECISIONS / f"{session}.json"
        cache[session] = json.loads(path.read_text()) if path.exists() else None
    day = cache[session]
    if day is None:
        return None
    index = entry_minute - 9 * 60 - 30
    record = day.get("bars", {}).get(str(index))
    table = day.get("q_f1")
    if not isinstance(record, dict) or not table:
        return None
    return record, table


# ---------------------------------------------------------------- bracket


def first_passage(
    minutes: list[int],
    bars: list[tuple],
    trade: dict,
    target_frac: float,
    deadline_minute: int,
) -> dict:
    """Fraction bracket from the journalled entry fill, adverse-first.

    The entry price already carries fc_t13's 1-tick adverse fill; bars are
    tested from the entry minute onward (the first bar whose ticks trade
    strictly after the 5m signal close). The deadline bar tests the
    bracket first, then flattens at its close -- the engine's ordering.
    """
    entry = trade["entry"]
    direction = trade["direction"]
    tgt = entry * (1 + direction * target_frac)
    stp = entry * (1 - direction * STP_F)
    i = bisect_left(minutes, trade["entry_minute"])
    exit_px, outcome, exit_min = bars[-1][3], "expired", minutes[-1]
    for j in range(i, len(minutes)):
        m = minutes[j]
        o, h, l, c = bars[j]
        if direction == 1:
            if l <= stp:
                exit_px, outcome, exit_min = stp - TICK, "stop", m
                break
            if h >= tgt:
                exit_px, outcome, exit_min = tgt, "hit", m
                break
        else:
            if h >= stp:
                exit_px, outcome, exit_min = stp + TICK, "stop", m
                break
            if l <= tgt:
                exit_px, outcome, exit_min = tgt, "hit", m
                break
        if m >= deadline_minute:
            exit_px, outcome, exit_min = c, "expired", m
            break
    pnl = direction * (exit_px - entry) * POINT_VALUE - RT_COST
    return {
        "outcome": outcome,
        "pnl": round(pnl, 2),
        "minutes_held": exit_min - trade["entry_minute"] + 1,
    }


# ---------------------------------------------------------------- panels


def t_stat(values: list[float]) -> float:
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    sd = statistics.stdev(values)
    return mean / (sd / math.sqrt(n)) if sd else 0.0


def panel(results: list[dict]) -> dict:
    n = len(results)
    if not n:
        return {"n": 0}
    hits = sum(r["outcome"] == "hit" for r in results)
    stops = sum(r["outcome"] == "stop" for r in results)
    expired = n - hits - stops
    pnls = [r["pnl"] for r in results]
    return {
        "n": n,
        "hit_rate": round(hits / n, 4),
        "stop_rate": round(stops / n, 4),
        "expired_rate": round(expired / n, 4),
        "ev": round(sum(pnls) / n, 2),
        "total": round(sum(pnls), 2),
        "t": round(t_stat(pnls), 2),
        "expired_ev": round(
            sum(r["pnl"] for r in results if r["outcome"] == "expired") / expired, 2
        )
        if expired
        else None,
    }


def quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("var/ssx"))
    args = parser.parse_args()

    trades, excluded = load_journal_trades()
    gamma = load_gamma()
    decision_cache: dict = {}
    bar_cache: dict[str, tuple[list[int], list[tuple]]] = {}

    def bars_for(session: str) -> tuple[list[int], list[tuple]]:
        if session not in bar_cache:
            bar_cache[session] = load_bars(session)
        return bar_cache[session]

    # 1/3. the ladder (V3-20 is one point on it) + the requested cell
    ladder: dict[int, list[dict]] = {pts: [] for pts in LADDER_PTS}
    v3_20_t13: list[dict] = []
    for trade in trades:
        minutes, bars = bars_for(trade["session"])
        for pts in LADDER_PTS:
            ladder[pts].append(
                first_passage(minutes, bars, trade, pts / P_REF, FLATTEN_MIN)
            )
        v3_20_t13.append(
            first_passage(
                minutes,
                bars,
                trade,
                20 / P_REF,
                min(FLATTEN_MIN, trade["entry_minute"] + T13_MINUTES - 1),
            )
        )
    v3_20 = ladder[20]

    # 2. time-to-hit for HIT_20
    hit_minutes = sorted(
        float(r["minutes_held"]) for r in v3_20 if r["outcome"] == "hit"
    )
    time_to_hit = {
        "n": len(hit_minutes),
        "mean": round(statistics.mean(hit_minutes), 1) if hit_minutes else None,
        "median": round(quantile(hit_minutes, 0.5), 1),
        "p75": round(quantile(hit_minutes, 0.75), 1),
        "p90": round(quantile(hit_minutes, 0.90), 1),
        "max": max(hit_minutes) if hit_minutes else None,
        "share_within_15m": round(sum(m <= 15 for m in hit_minutes) / len(hit_minutes), 4)
        if hit_minutes
        else None,
        "share_within_30m": round(sum(m <= 30 for m in hit_minutes) / len(hit_minutes), 4)
        if hit_minutes
        else None,
        "share_within_65m": round(sum(m <= 65 for m in hit_minutes) / len(hit_minutes), 4)
        if hit_minutes
        else None,
    }

    # 4. stop anatomy (V3-20)
    stop_sessions = [
        t["session"] for t, r in zip(trades, v3_20) if r["outcome"] == "stop"
    ]
    stops_per_month: dict[str, int] = defaultdict(int)
    for session in stop_sessions:
        stops_per_month[session[:7]] += 1
    all_sessions = sorted({t["session"] for t in trades})
    max_5d = 0
    worst_window = None
    for i in range(len(all_sessions)):
        window = set(all_sessions[i : i + 5])
        count = sum(s in window for s in stop_sessions)
        if count > max_5d:
            max_5d = count
            worst_window = (min(window), max(window))
    stop_loss = sum(-r["pnl"] for r in v3_20 if r["outcome"] == "stop" and r["pnl"] < 0)
    total_loss = sum(-r["pnl"] for r in v3_20 if r["pnl"] < 0)
    worst_5d_dollars = 0.0
    if worst_window:
        worst_5d_dollars = sum(
            -r["pnl"]
            for t, r in zip(trades, v3_20)
            if r["outcome"] == "stop" and worst_window[0] <= t["session"] <= worst_window[1]
        )
    stop_anatomy = {
        "stops": len(stop_sessions),
        "stops_per_month": dict(sorted(stops_per_month.items())),
        "max_stops_5_session_window": max_5d,
        "worst_window": worst_window,
        "worst_window_stop_dollars": round(worst_5d_dollars, 2),
        "avg_stop_pnl": round(
            statistics.mean([r["pnl"] for r in v3_20 if r["outcome"] == "stop"]), 2
        )
        if stop_sessions
        else None,
        "stop_share_of_total_loss": round(stop_loss / total_loss, 4) if total_loss else None,
    }

    # 5. conditional cuts, one at a time (V3-20)
    f1_buckets: dict[str, list[dict]] = defaultdict(list)
    hour_buckets: dict[int, list[dict]] = defaultdict(list)
    gamma_buckets: dict[str, list[dict]] = defaultdict(list)
    for trade, result in zip(trades, v3_20):
        joined = decision_record(decision_cache, trade["session"], trade["entry_minute"])
        if joined is not None:
            record, table = joined
            f1 = abs(record.get("f1_5") or 0.0)
            if f1 >= float(table["85"]):
                f1_buckets["Q85+"].append(result)
            elif f1 >= float(table["80"]):
                f1_buckets["Q80-85"].append(result)
            else:
                f1_buckets["Q70-80"].append(result)
        else:
            f1_buckets["unjoined"].append(result)
        hour_buckets[trade["entry_hour"]].append(result)
        gamma_buckets[gamma.get(trade["session"], "unknown")].append(result)

    # the incumbent, same trades, from its own journal (net of $10 RT)
    fc_pnls = [t["fc_t13_pnl"] for t in trades]
    fc_t13 = {
        "n": len(fc_pnls),
        "ev": round(statistics.mean(fc_pnls), 2),
        "total": round(sum(fc_pnls), 2),
        "win_rate": round(sum(p > 0 for p in fc_pnls) / len(fc_pnls), 4),
        "t": round(t_stat(fc_pnls), 2),
    }

    result = {
        "spec": "SSX-V3 v1.0 Phase 1",
        "n_entries": len(trades),
        "n_excluded_no_fixture": excluded,
        "sessions": [all_sessions[0], all_sessions[-1]],
        "geometry": {
            "target_frac_20": round(20 / P_REF, 8),
            "stop_frac": STP_F,
            "p_ref": P_REF,
            "resolution": "1m bars, adverse-first; stop +1 tick adverse; $10/RT",
        },
        "unconditional_v3_20": panel(v3_20),
        "v3_20_t13_backstop": panel(v3_20_t13),
        "ladder": {str(pts): panel(ladder[pts]) for pts in LADDER_PTS},
        "time_to_hit_20": time_to_hit,
        "stop_anatomy_v3_20": stop_anatomy,
        "cuts_f1_percentile": {k: panel(v) for k, v in sorted(f1_buckets.items())},
        "cuts_entry_hour": {str(k): panel(v) for k, v in sorted(hour_buckets.items())},
        "cuts_gamma": {k: panel(v) for k, v in sorted(gamma_buckets.items())},
        "fc_t13_same_entries": fc_t13,
    }

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "v3_phase1.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"n = {len(trades)} entries ({excluded} excluded, no fixture coverage)")
    print(f"\nV3-20 unconditional: {result['unconditional_v3_20']}")
    print(f"V3-20 + 13-bar backstop: {result['v3_20_t13_backstop']}")
    print("\nladder:")
    for pts in LADDER_PTS:
        print(f"  {pts:>3} pts: {result['ladder'][str(pts)]}")
    print(f"\ntime-to-hit (20): {time_to_hit}")
    print(f"\nstop anatomy: {stop_anatomy}")
    print("\n|F1_5| percentile cuts:")
    for k, v in sorted(f1_buckets.items()):
        print(f"  {k}: {panel(v)}")
    print("\nentry-hour cuts:")
    for k, v in sorted(hour_buckets.items()):
        print(f"  {k:>2}h: {panel(v)}")
    print("\ngamma cuts:")
    for k, v in sorted(gamma_buckets.items()):
        print(f"  {k}: {panel(v)}")
    print(f"\nfc_t13, same entries: {fc_t13}")
    print(f"\nwritten -> {out_path}")


if __name__ == "__main__":
    main()
