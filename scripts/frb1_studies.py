"""FRB-1: the four $0 orderflow studies (FAR, VCT, SSF, DDR stage 1).

Overlay-first doctrine: every study is a descriptive join against the
existing flow files, decision files and the fc_t13 develop journal.
Nothing trades; nothing is swept beyond the thresholds pre-declared in
the batch spec; each subcommand prints its panel and writes a JSON
summary for the batch report.

All series are walk-forward: any percentile/threshold consulted at time
t derives from data strictly before t (trailing-session windows), and
per-trade tags use the trade's entry bar, never hindsight.

Usage:
  uv run python scripts/frb1_studies.py far   [--out var/frb1]
  uv run python scripts/frb1_studies.py vct   [--out var/frb1]
  uv run python scripts/frb1_studies.py ssf   [--out var/frb1]
  uv run python scripts/frb1_studies.py ddr   [--out var/frb1]
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FLOW = Path("var/flow")
DECISIONS = Path("var/decisions/k3m")
JOURNAL = Path("var/tfr/fc_t13/journal")
GAMMA = Path("var/gamma/regimes.jsonl")


# ---------------------------------------------------------------- shared


def sessions() -> list[str]:
    return sorted(p.stem for p in FLOW.glob("*.json"))


def minutes_of(session: str) -> dict[int, dict]:
    payload = json.loads((FLOW / f"{session}.json").read_text())
    return {int(k): v for k, v in payload["minutes"].items()}


def signed_minute_flow(minutes: dict[int, dict]) -> dict[int, float]:
    out = {}
    for index, m in minutes.items():
        if m["vol"]:
            out[index] = (m["buy"] - m["sell"]) / m["vol"]
    return out


def trades_with_entry_bars() -> list[dict]:
    """fc_t13 develop trades with their entry 5m bar index and quarter."""
    rows = []
    for path in sorted(JOURNAL.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if record.get("event") != "position_closed":
                continue
            entry = datetime.fromisoformat(record["entry_time"]).astimezone(ET)
            index = (entry.hour - 9) * 60 + entry.minute - 30
            month = int(path.stem[5:7])
            rows.append(
                {
                    "session": path.stem,
                    "index": index,
                    "direction": 1 if record["direction"] == "LONG" else -1,
                    "pnl": float(record["realised_pnl"]),
                    "quarter": f"{path.stem[:4]}Q{(month - 1) // 3 + 1}",
                }
            )
    return rows


def bootstrap_p(a: list[float], b: list[float], resamples: int = 10_000) -> float:
    """P(mean(a) - mean(b) <= 0) under resampling, one-sided."""
    rng = random.Random(7)
    if not a or not b or (sum(a) / len(a) - sum(b) / len(b)) <= 0:
        return 1.0
    hits = 0
    for _ in range(resamples):
        sa = [rng.choice(a) for _ in a]
        sb = [rng.choice(b) for _ in b]
        hits += int(sum(sa) / len(sa) - sum(sb) / len(sb) <= 0)
    return hits / resamples


def tercile_split(tagged: list[tuple[float, float]], name: str) -> dict:
    """tagged = [(pctile_rank, pnl)]. Terciles at 1/3, 2/3."""
    buckets: dict[str, list[float]] = {"BOT": [], "MID": [], "TOP": []}
    for rank, pnl in tagged:
        key = "BOT" if rank <= 1 / 3 else ("TOP" if rank >= 2 / 3 else "MID")
        buckets[key].append(pnl)
    print(f"\n{name}: EV by tercile at entry")
    for key in ("BOT", "MID", "TOP"):
        values = buckets[key]
        if values:
            wins = sum(1 for v in values if v > 0)
            print(
                f"  {key}: n={len(values):<4} EV=${sum(values) / len(values):>8,.2f} "
                f"win={wins / len(values):.0%}"
            )
    top, bot = buckets["TOP"], buckets["BOT"]
    result = {"buckets": {k: [len(v), sum(v) / len(v) if v else None] for k, v in buckets.items()}}
    if len(top) >= 5 and len(bot) >= 5:
        diff = sum(top) / len(top) - sum(bot) / len(bot)
        p = bootstrap_p(top, bot) if diff > 0 else bootstrap_p(bot, top)
        direction = "TOP>BOT" if diff > 0 else "BOT>TOP"
        print(
            f"  {direction} diff=${abs(diff):,.2f}  p={p:.4f}  "
            "(gate: >=$150, n>=60/bucket, p<0.10)"
        )
        result.update({"diff": diff, "p": p, "direction": direction})
    return result


def t_stat(values: list[float]) -> float:
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    return 0.0 if sd == 0 else mean / (sd / math.sqrt(n))


# ------------------------------------------------------------------- FAR


def study_far(window: int = 90) -> dict:
    """AR(1) of signed 1m flow over the trailing `window` minutes,
    percentile-ranked against the trailing 60 sessions, tagged at entries."""
    trailing: deque[list[float]] = deque(maxlen=60)
    series_by_session: dict[str, dict[int, float]] = {}
    for session in sessions():
        flow = signed_minute_flow(minutes_of(session))
        indices = sorted(flow)
        values = [flow[i] for i in indices]
        far_at: dict[int, float] = {}
        today: list[float] = []
        for pos in range(window, len(values)):
            chunk = values[pos - window : pos]
            mean = sum(chunk) / len(chunk)
            num = sum(
                (chunk[i] - mean) * (chunk[i - 1] - mean) for i in range(1, len(chunk))
            )
            den = sum((v - mean) ** 2 for v in chunk)
            if den > 0:
                far = num / den
                far_at[indices[pos]] = far
                today.append(far)
        # walk-forward percentile rank vs trailing sessions
        flat = sorted(v for day in trailing for v in day)
        if flat:
            series_by_session[session] = {
                i: sum(1 for v in flat if v <= far) / len(flat) for i, far in far_at.items()
            }
        trailing.append(today)

    tagged = []
    for trade in trades_with_entry_bars():
        ranks = series_by_session.get(trade["session"])
        if not ranks:
            continue
        eligible = [i for i in ranks if i <= trade["index"]]
        if eligible:
            tagged.append((ranks[max(eligible)], trade["pnl"]))
    print(f"FAR study: {len(tagged)} trades tagged (window={window}m)")
    return tercile_split(tagged, "FAR (flow persistence)")


# ------------------------------------------------------------------- VCT


def study_vct(buckets_per_day: int = 50, trailing_buckets: int = 50) -> dict:
    """Volume-clock imbalance toxicity, percentile-ranked walk-forward."""
    vol_hist: deque[float] = deque(maxlen=20)
    vct_hist: deque[list[float]] = deque(maxlen=60)
    series_by_session: dict[str, dict[int, float]] = {}
    day_open_vct: dict[str, float] = {}

    for session in sessions():
        minutes = minutes_of(session)
        day_vol = sum(m["vol"] for m in minutes.values())
        if vol_hist:
            ordered = sorted(vol_hist)
            bucket_size = ordered[len(ordered) // 2] / buckets_per_day
        else:
            bucket_size = day_vol / buckets_per_day if day_vol else 1
        # build equal-volume buckets at minute granularity
        imbalances: list[float] = []
        acc_vol = 0.0
        acc_signed = 0.0
        vct_at: dict[int, float] = {}
        today_samples: list[float] = []
        for index in sorted(minutes):
            m = minutes[index]
            acc_vol += m["vol"]
            acc_signed += m["buy"] - m["sell"]
            while acc_vol >= bucket_size and bucket_size > 0:
                share = bucket_size / acc_vol
                imbalances.append(abs(acc_signed * share) / bucket_size)
                acc_signed -= acc_signed * share
                acc_vol -= bucket_size
            if len(imbalances) >= 10:
                recent = imbalances[-trailing_buckets:]
                vct_at[index] = sum(recent) / len(recent)
                today_samples.append(vct_at[index])
        flat = sorted(v for day in vct_hist for v in day)
        if flat and vct_at:
            ranks = {
                i: sum(1 for v in flat if v <= x) / len(flat) for i, x in vct_at.items()
            }
            series_by_session[session] = ranks
            first_hour = [ranks[i] for i in ranks if i <= 60]
            if first_hour:
                day_open_vct[session] = sum(first_hour) / len(first_hour)
        vct_hist.append(today_samples)
        vol_hist.append(day_vol)

    tagged = []
    day_tagged = []
    for trade in trades_with_entry_bars():
        ranks = series_by_session.get(trade["session"])
        if ranks:
            eligible = [i for i in ranks if i <= trade["index"]]
            if eligible:
                tagged.append((ranks[max(eligible)], trade["pnl"]))
        if trade["session"] in day_open_vct:
            day_tagged.append((day_open_vct[trade["session"]], trade["pnl"]))
    print(f"VCT study: {len(tagged)} trades tagged intraday, {len(day_tagged)} by day-open")
    result = {"intraday": tercile_split(tagged, "VCT intraday (toxicity at entry)")}
    result["day_open"] = tercile_split(day_tagged, "VCT day-open")

    # gamma cross-tab
    if GAMMA.exists():
        regimes = {
            json.loads(line)["date"]: json.loads(line)["regime"]
            for line in GAMMA.read_text().splitlines()
        }
        cross: dict[tuple[str, str], int] = defaultdict(int)
        for session, vct in day_open_vct.items():
            tier = "BOT" if vct <= 1 / 3 else ("TOP" if vct >= 2 / 3 else "MID")
            cross[(tier, regimes.get(session, "?"))] += 1
        print("\nVCT day-open tier x gamma regime (session counts):")
        for (tier, regime), n in sorted(cross.items()):
            print(f"  {tier} x {regime}: {n}")
        result["gamma_crosstab"] = {f"{t}|{g}": n for (t, g), n in cross.items()}
    return result


# ------------------------------------------------------------------- SSF


def study_ssf() -> dict:
    """Size-split flow: big-lot vs small-lot disagreement events."""
    p90_hist: deque[int] = deque(maxlen=20)
    p50_hist: deque[int] = deque(maxlen=20)
    big_hist: deque[list[float]] = deque(maxlen=60)
    small_hist: deque[list[float]] = deque(maxlen=60)
    cuts = (3, 5, 10, 20)

    events = []  # (session, index, big_sign, forward returns dict)
    by_session_bars: dict[str, dict[int, tuple[float, float, float]]] = {}

    for session in sessions():
        payload = json.loads((FLOW / f"{session}.json").read_text())
        qa = payload["qa"]
        minutes = {int(k): v for k, v in payload["minutes"].items()}
        p90 = sorted(p90_hist)[len(p90_hist) // 2] if p90_hist else 10
        p50 = sorted(p50_hist)[len(p50_hist) // 2] if p50_hist else 3
        big_cut = min(cuts, key=lambda c: abs(c - p90))
        small_cut = min(cuts, key=lambda c: abs(c - p50))
        flat_big = sorted(v for day in big_hist for v in day)
        flat_small = sorted(v for day in small_hist for v in day)
        today_big: list[float] = []
        today_small: list[float] = []
        bars: dict[int, tuple[float, float, float]] = {}
        closes: dict[int, float] = {}
        for end in range(5, 391, 5):
            window = [minutes[i] for i in range(end - 4, end + 1) if i in minutes]
            if not window:
                continue
            closes[end] = float(window[-1]["close"])
            big_buy = sum(m[f"buy_ge{big_cut}"] for m in window)
            big_sell = sum(m[f"sell_ge{big_cut}"] for m in window)
            small_buy = sum(m["buy"] - m[f"buy_ge{small_cut}"] for m in window)
            small_sell = sum(m["sell"] - m[f"sell_ge{small_cut}"] for m in window)
            f1_big = (big_buy - big_sell) / (big_buy + big_sell) if big_buy + big_sell else 0.0
            f1_small = (
                (small_buy - small_sell) / (small_buy + small_sell)
                if small_buy + small_sell
                else 0.0
            )
            today_big.append(f1_big)
            today_small.append(f1_small)

            def z(value: float, flat: list[float]) -> float:
                if len(flat) < 100:
                    return 0.0
                mean = sum(flat) / len(flat)
                sd = math.sqrt(sum((v - mean) ** 2 for v in flat) / len(flat))
                return 0.0 if sd == 0 else (value - mean) / sd

            bars[end] = (f1_big, f1_small, z(f1_big, flat_big))
            zb, zs = z(f1_big, flat_big), z(f1_small, flat_small)
            if (
                flat_big
                and f1_big * f1_small < 0
                and abs(zb) >= 0.5
                and abs(zs) >= 0.5
                and 5 <= end <= 330
            ):
                events.append({"session": session, "index": end, "big_sign": 1 if f1_big > 0 else -1})
        by_session_bars[session] = bars
        # forward returns for this session's events
        for event in [e for e in events if e["session"] == session]:
            fwd = {}
            for horizon in (1, 3, 6, 13):
                target = event["index"] + 5 * horizon
                if target in closes and event["index"] in closes:
                    fwd[horizon] = (
                        (closes[target] - closes[event["index"]]) * event["big_sign"]
                    )
            event["fwd"] = fwd
        big_hist.append(today_big)
        small_hist.append(today_small)
        p90_hist.append(qa["size_p90"])
        p50_hist.append(qa["size_p50"])

    print(f"SSF study: {len(events)} disagreement events")
    result: dict = {"events": len(events)}
    print("forward returns in BIG-lot direction (points):")
    for horizon in (1, 3, 6, 13):
        values = [e["fwd"][horizon] for e in events if e.get("fwd", {}).get(horizon) is not None]
        if values:
            print(
                f"  +{horizon} bars: n={len(values):<5} mean={sum(values) / len(values):>7.2f}  "
                f"T={t_stat(values):.2f}"
            )
            result[f"h{horizon}"] = {"n": len(values), "mean": sum(values) / len(values), "t": t_stat(values)}

    # overlay: fc_t13 entries by big-lot agreement at entry bar
    agree, disagree = [], []
    for trade in trades_with_entry_bars():
        bars = by_session_bars.get(trade["session"], {})
        bar = bars.get(trade["index"] - trade["index"] % 5 or 5)
        if bar is None:
            bar = bars.get(max((i for i in bars if i <= trade["index"]), default=None) or -1)
        if bar is None:
            continue
        f1_big = bar[0]
        if f1_big == 0:
            continue
        (agree if (f1_big > 0) == (trade["direction"] > 0) else disagree).append(trade["pnl"])
    if agree and disagree:
        ev_a, ev_d = sum(agree) / len(agree), sum(disagree) / len(disagree)
        p = bootstrap_p(agree, disagree) if ev_a > ev_d else bootstrap_p(disagree, agree)
        print(
            f"overlay: big-lot AGREE n={len(agree)} EV=${ev_a:,.2f} | "
            f"DISAGREE n={len(disagree)} EV=${ev_d:,.2f} | p={p:.4f}"
        )
        result["overlay"] = {"agree": [len(agree), ev_a], "disagree": [len(disagree), ev_d], "p": p}
    return result


# ------------------------------------------------------------------- DDR


def study_ddr() -> dict:
    """Delta-divergence forward-return panels, both declared thresholds."""
    result: dict = {}
    for threshold in (1.0, 1.5):
        events = []
        for path in sorted(DECISIONS.glob("*.json")):
            day = json.loads(path.read_text())
            if not day.get("model"):
                continue
            bars = {int(k): v for k, v in day.get("bars", {}).items()}
            closes = {i: b["close"] for i, b in bars.items()}
            for index, bar in bars.items():
                div = bar.get("div")
                if div is None or abs(div) < threshold or not (5 <= index <= 330):
                    continue
                fade_sign = -1 if div > 0 else 1  # against the divergence direction
                fwd = {}
                for horizon in (1, 3, 6, 13):
                    target = index + 5 * horizon
                    if target in closes:
                        fwd[horizon] = (closes[target] - closes[index]) * fade_sign
                events.append({"session": path.stem, "fwd": fwd, "q": path.stem[:7]})
        len(events) * 252 / max(1, len(set(e["session"] for e in events)) and 200)
        print(f"\nDDR |div| >= {threshold}: {len(events)} events (~{len(events) * 252 // 200}/year)")
        panel = {}
        for horizon in (1, 3, 6, 13):
            values = [e["fwd"][horizon] for e in events if e["fwd"].get(horizon) is not None]
            if values:
                print(
                    f"  +{horizon} bars against divergence: n={len(values):<5} "
                    f"mean={sum(values) / len(values):>7.2f} pts  T={t_stat(values):.2f}"
                )
                panel[f"h{horizon}"] = {
                    "n": len(values),
                    "mean": sum(values) / len(values),
                    "t": t_stat(values),
                }
        result[str(threshold)] = {"events": len(events), "panel": panel}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", choices=["far", "vct", "ssf", "ddr"])
    parser.add_argument("--window", type=int, default=90, help="FAR AR(1) window, minutes")
    parser.add_argument("--out", type=Path, default=Path("var/frb1"))
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    if args.study == "far":
        result = study_far(window=args.window)
    else:
        runner = {"vct": study_vct, "ssf": study_ssf, "ddr": study_ddr}[args.study]
        result = runner()
    (args.out / f"{args.study}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
