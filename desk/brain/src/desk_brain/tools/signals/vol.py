"""A10 — volatility regime. Gamma regime itself comes from the archive
(state/gamma.py, validated as a risk regime only); this module covers the
realized-vol side. Implied vol / expected move need an options feed the desk
doesn't have in v1 — callers report that honestly instead of proxying."""

from __future__ import annotations

from math import log, sqrt
from typing import Any


def true_ranges(daily: list[dict]) -> list[float]:
    """True range per day from daily {'h','l','c'} rows (chronological)."""
    out: list[float] = []
    prev_close: float | None = None
    for d in daily:
        tr = d["h"] - d["l"]
        if prev_close is not None:
            tr = max(tr, abs(d["h"] - prev_close), abs(d["l"] - prev_close))
        out.append(tr)
        prev_close = d["c"]
    return out


def atr(daily: list[dict], n: int) -> float | None:
    """A10.3 — mean true range over the last n days (fewer if that's all we
    have; None below 3 — an 'ATR' of two days is noise wearing a suit)."""
    trs = true_ranges(daily)[-n:]
    if len(trs) < 3:
        return None
    return round(sum(trs) / len(trs), 2)


def realized_vol_1m(bars: list[dict], window: int) -> float | None:
    """Annualized close-to-close vol over the last `window` 1m bars, %."""
    closes = [b["c"] for b in bars[-(window + 1):]]
    rets = [log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return round(sqrt(var) * sqrt(390 * 252) * 100, 2)


def vol_of_vol(bars_1m: list[dict], window: int) -> dict[str, Any] | None:
    """A10.4 — recent 1m realized vol vs the session's: a regime change in
    progress reads as the recent window running hot."""
    recent = realized_vol_1m(bars_1m, window)
    session = realized_vol_1m(bars_1m, len(bars_1m))
    if recent is None or session is None or session == 0:
        return None
    ratio = recent / session
    return {"recent": recent, "session": session, "ratio": round(ratio, 2),
            "spiking": ratio >= 1.5}


def rv_vs_iv(realized: float | None, implied: float | None) -> dict[str, Any] | None:
    """A10.2 — moving more or less than priced. Returns None without an IV
    feed; never proxied."""
    if realized is None or implied is None:
        return None
    return {"realized": realized, "implied": implied,
            "read": "moving more than priced" if realized > implied else "moving less than priced"}
