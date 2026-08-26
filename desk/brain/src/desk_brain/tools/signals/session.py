"""A9 — session and day context. The day-type classifier here is the
addendum's rule-based probability blend (open type, IB, gap, CVD slope, VA
position) — a regime GUESS, discretionary by tag, not a model with a study."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def gap_read(rth_open: float | None, prior_close: float | None, atr: float | None,
             session_high: float | None, session_low: float | None) -> dict[str, Any] | None:
    """A9.1 — gap size in points and vs ATR, plus fill status this session."""
    if rth_open is None or prior_close is None:
        return None
    gap = rth_open - prior_close
    out: dict[str, Any] = {"points": round(gap, 2), "direction": "up" if gap > 0 else "down" if gap < 0 else "flat"}
    if atr:
        out["vs_atr"] = round(abs(gap) / atr, 2)
    filled = None
    pct = None
    if gap > 0 and session_low is not None:
        pct = min(1.0, max(0.0, (rth_open - session_low) / gap)) if gap else None
    elif gap < 0 and session_high is not None:
        pct = min(1.0, max(0.0, (session_high - rth_open) / -gap))
    if pct is not None:
        filled = pct >= 1.0
        out["fill_pct"] = round(pct * 100)
    out["filled"] = filled
    return out


def open_type(bars_1m_rth: list[dict], window_min: int) -> dict[str, Any] | None:
    """A9.2 — first N minutes: open-drive / open-test-drive /
    open-rejection-reverse / open-auction, by simple range-and-retrace rules."""
    window = bars_1m_rth[:window_min]
    if len(window) < max(5, window_min // 3):
        return None
    o = window[0]["o"]
    closes = [b["c"] for b in window]
    hi = max(b["h"] for b in window)
    lo = min(b["l"] for b in window)
    rng = hi - lo
    if rng <= 0:
        return None
    end = closes[-1]
    up_frac = (end - lo) / rng
    crossed_open = any(b["l"] < o < b["h"] for b in window[len(window) // 2:])
    one_way = (end - o) != 0 and abs(end - o) >= 0.7 * rng
    if one_way and not crossed_open:
        kind = "open-drive"
    elif one_way:
        kind = "open-test-drive"
    elif (up_frac > 0.7 and closes[0] < o) or (up_frac < 0.3 and closes[0] > o):
        kind = "open-rejection-reverse"
    else:
        kind = "open-auction"
    return {"type": kind, "direction": "up" if end > o else "down", "conviction": "high" if kind == "open-drive" else "medium" if kind.endswith("drive") else "low"}


def open_location(rth_open: float | None, prior_va: dict | None, on_high: float | None, on_low: float | None) -> dict[str, Any] | None:
    """A9.3 — where the open sat relative to prior value and the ON range."""
    if rth_open is None:
        return None
    out: dict[str, Any] = {"open": rth_open}
    if prior_va and prior_va.get("vah") is not None and prior_va.get("val") is not None:
        out["vs_value"] = "above" if rth_open > prior_va["vah"] else "below" if rth_open < prior_va["val"] else "inside"
    if on_high is not None and on_low is not None:
        out["vs_overnight"] = "above" if rth_open > on_high else "below" if rth_open < on_low else "inside"
    return out


def range_vs_atr(session_high: float | None, session_low: float | None, atr: float | None) -> dict[str, Any] | None:
    """A9.4 — today's range as a fraction of ATR: has the day used itself up."""
    if session_high is None or session_low is None or not atr:
        return None
    frac = (session_high - session_low) / atr
    return {"range_pts": round(session_high - session_low, 2), "atr_frac": round(frac, 2),
            "used_up": frac >= 1.0}


def range_extension(ib: dict | None, session_high: float | None, session_low: float | None) -> dict[str, Any] | None:
    """A9.5 — extension beyond the initial balance, which side."""
    if not ib or not ib.get("complete") or session_high is None or session_low is None:
        return None
    up = session_high > ib["high"]
    down = session_low < ib["low"]
    return {
        "extended": up or down,
        "side": "both" if up and down else "up" if up else "down" if down else None,
        "up_pts": round(max(0.0, session_high - ib["high"]), 2),
        "down_pts": round(max(0.0, ib["low"] - session_low), 2),
    }


def day_type(open_typ: dict | None, ext: dict | None, gap: dict | None,
             cvd_slope_val: float | None, va_position: str | None) -> dict[str, float]:
    """A9.6 — trend / range / reversal probabilities from crude vote weights.
    A guess with a confidence, not an oracle; two-sided by construction when
    the inputs disagree."""
    trend = rng = rev = 1.0  # priors
    if open_typ:
        if open_typ["type"] == "open-drive":
            trend += 2
        elif open_typ["type"] == "open-test-drive":
            trend += 1
        elif open_typ["type"] == "open-auction":
            rng += 2
        elif open_typ["type"] == "open-rejection-reverse":
            rev += 2
    if ext:
        if ext["extended"] and ext["side"] in ("up", "down"):
            trend += 1.5
        elif ext["side"] == "both":
            rng += 1
        elif not ext["extended"]:
            rng += 1.5
    if gap and gap.get("filled") is False and abs(gap.get("points") or 0) > 0:
        trend += 0.5
    elif gap and gap.get("filled"):
        rng += 0.5
    if cvd_slope_val is not None and va_position:
        with_value = (cvd_slope_val > 0 and va_position == "above") or (cvd_slope_val < 0 and va_position == "below")
        against = (cvd_slope_val < 0 and va_position == "above") or (cvd_slope_val > 0 and va_position == "below")
        if with_value:
            trend += 1
        elif against:
            rev += 1.5
    total = trend + rng + rev
    return {"trend": round(trend / total, 2), "range": round(rng / total, 2), "reversal": round(rev / total, 2)}


def time_bucket(now_et: datetime, buckets: dict[str, list[str]]) -> dict[str, Any]:
    """A9.7 — which behavior bucket we're in and minutes to the next one."""
    t = now_et.timetz()
    order = list(buckets.items())
    current = None
    next_start = None
    for i, (name, (start, end)) in enumerate(order):
        s_h, s_m = map(int, start.split(":"))
        e_h, e_m = map(int, end.split(":"))
        if (t.hour, t.minute) >= (s_h, s_m) and (t.hour, t.minute) < (e_h, e_m):
            current = name
            if i + 1 < len(order):
                n_h, n_m = map(int, order[i + 1][1][0].split(":"))
                next_start = (n_h * 60 + n_m) - (t.hour * 60 + t.minute)
            break
    return {"bucket": current, "minutes_to_next": next_start}


def minutes_to_event(events: list[dict], now_et: datetime, impact: str = "high") -> dict[str, Any] | None:
    """A9.8 — next high-impact econ event today, in minutes. Don't be in a
    trade into it."""
    best: tuple[float, dict] | None = None
    for ev in events:
        if impact and (ev.get("impact") or "").lower() != impact:
            continue
        try:
            h, m = str(ev["event_time_et"]).split(":")[:2]
            ev_min = int(h) * 60 + int(m)
        except (KeyError, ValueError):
            continue
        delta = ev_min - (now_et.hour * 60 + now_et.minute)
        if delta >= 0 and (best is None or delta < best[0]):
            best = (delta, ev)
    if best is None:
        return None
    return {"minutes": int(best[0]), "name": best[1].get("name"), "impact": best[1].get("impact")}
