"""A1 — aggressor flow. Bar delta / impulse / Q-rank are computed by the
engine (nq_agent.flow, verbatim fc_t13 math); this module derives the rest
from the bar series and the live tick window."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import linreg_slope


def cvd_series(bars: list[dict]) -> list[float]:
    """A1.2 — running Σ of bar delta over the given bars (whatever anchor the
    caller chose: RTH bars in = RTH CVD out)."""
    out: list[float] = []
    acc = 0.0
    for b in bars:
        acc += b.get("delta") or 0
        out.append(acc)
    return out


def cvd_slope(bars: list[dict], slope_bars: int) -> float | None:
    """A1.9 — regression slope of CVD over the last `slope_bars` bars."""
    series = cvd_series(bars)
    return linreg_slope(series[-slope_bars:])


def delta_vol_ratio(bar: dict) -> float | None:
    """A1.6 — bar delta ÷ bar volume, −1…+1."""
    vol = bar.get("vol") or 0
    if not vol:
        return None
    return round((bar.get("delta") or 0) / vol, 3)


def delta_persistence(bars: list[dict]) -> dict[str, Any]:
    """A1.7 — current run of same-sign delta bars and the session's longest."""
    current = longest = 0
    current_sign = 0
    longest_sign = 0
    run = 0
    sign = 0
    for b in bars:
        d = b.get("delta") or 0
        s = 1 if d > 0 else -1 if d < 0 else 0
        if s != 0 and s == sign:
            run += 1
        else:
            run = 1 if s != 0 else 0
            sign = s
        if run > longest:
            longest, longest_sign = run, s
    current, current_sign = run, sign
    return {
        "current_run": current,
        "current_sign": "buy" if current_sign > 0 else "sell" if current_sign < 0 else None,
        "longest_run": longest,
        "longest_sign": "buy" if longest_sign > 0 else "sell" if longest_sign < 0 else None,
    }


def cvd_divergence(bars: list[dict], lookback: int) -> dict[str, Any] | None:
    """A1.8 — price makes a new extreme over `lookback` bars but CVD does not
    (or vice versa). Returns the divergence found at the latest bar, or None."""
    if len(bars) < max(3, lookback):
        return None
    window = bars[-lookback:]
    cvd = cvd_series(bars)[-lookback:]
    last = window[-1]
    prior_high = max(b["h"] for b in window[:-1])
    prior_low = min(b["l"] for b in window[:-1])
    prior_cvd_max = max(cvd[:-1])
    prior_cvd_min = min(cvd[:-1])
    if last["h"] > prior_high and cvd[-1] <= prior_cvd_max:
        return {"kind": "bearish", "read": "new price high without new buying high"}
    if last["l"] < prior_low and cvd[-1] >= prior_cvd_min:
        return {"kind": "bullish", "read": "new price low without new selling low"}
    return None


def delta_at_extremes(bars: list[dict]) -> dict[str, Any]:
    """A1.10 — delta of the bar that set the session high / low, and the bar
    after it. Conviction or fumes at the extreme."""
    out: dict[str, Any] = {"high": None, "low": None}
    if not bars:
        return out
    hi_i = max(range(len(bars)), key=lambda i: bars[i]["h"])
    lo_i = min(range(len(bars)), key=lambda i: bars[i]["l"])
    for name, i in (("high", hi_i), ("low", lo_i)):
        out[name] = {
            "price": bars[i]["h"] if name == "high" else bars[i]["l"],
            "delta": bars[i].get("delta"),
            "next_delta": bars[i + 1].get("delta") if i + 1 < len(bars) else None,
        }
    return out


def delta_rate(ticks: Sequence[tuple], now_s: float, windows_s: Sequence[int]) -> dict[str, float | None]:
    """A1.5 — signed aggressor delta per second over each trailing window."""
    out: dict[str, float | None] = {}
    for w in windows_s:
        cutoff = now_s - w
        vols = [size * d for ts, _p, size, d in ticks if ts >= cutoff]
        out[f"{w}s"] = round(sum(vols) / w, 2) if vols else None
    return out
