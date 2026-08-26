"""A5 — depth / DOM signals over Tradovate book snapshots.

The noisiest and most spoofable family: weak evidence by construction, never
the sole basis of an opinion (the composite reads enforce that). Pure
functions over book snapshots so they are testable now; the live depth feed
is optional in v1 — when it isn't connected the tools report unavailable
rather than guessing.

book = {"ts": epoch_s, "bids": [[price, size], ...], "asks": [[price, size], ...]}
with best price first on each side."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

TICK = 0.25


def book_imbalance(book: dict, top_levels: int) -> float | None:
    """A5.1 — Σ bid size ÷ Σ ask size over the top levels. >1 = passive lean
    to the bid. Weak evidence; spoofable."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid_sz = sum(s for _, s in bids[:top_levels])
    ask_sz = sum(s for _, s in asks[:top_levels])
    if ask_sz <= 0:
        return None
    return round(bid_sz / ask_sz, 2)


def resting_size(book: dict, price: float) -> dict[str, Any] | None:
    """A5.2 — displayed size at a specific price, and which side it is."""
    for side in ("bids", "asks"):
        for p, s in book.get(side) or []:
            if abs(p - price) < 1e-9:
                return {"price": price, "side": side[:-1], "size": s}
    return None


def _traded_at(prints: Sequence[tuple], price: float, t0: float, t1: float) -> float:
    return sum(sz for ts, p, sz, _d in prints if t0 <= ts <= t1 and abs(p - price) < 1e-9)


def liquidity_pull(
    books: Sequence[dict], prints: Sequence[tuple], price: float, drop_frac: float, window_s: float
) -> dict[str, Any] | None:
    """A5.3 — displayed size at `price` drops > drop_frac within window_s
    without prints filling it: the wall was pulled."""
    if len(books) < 2:
        return None
    latest = books[-1]
    t1 = latest["ts"]
    earlier = [b for b in books if t1 - b["ts"] <= window_s]
    if len(earlier) < 2:
        return None
    first, last = resting_size(earlier[0], price), resting_size(latest, price)
    size0 = first["size"] if first else 0
    size1 = last["size"] if last else 0
    if size0 <= 0 or size1 > (1 - drop_frac) * size0:
        return None
    filled = _traded_at(prints, price, earlier[0]["ts"], t1)
    if filled >= (size0 - size1) * 0.5:
        return None  # it traded, it wasn't pulled
    return {"price": price, "was": size0, "now": size1, "filled": filled, "read": "wall pulled, not filled"}


def liquidity_add(books: Sequence[dict], price: float, add_mult: float, window_s: float) -> dict[str, Any] | None:
    """A5.4 — size at a level grows > add_mult× within window_s: defense or bait."""
    if len(books) < 2:
        return None
    latest = books[-1]
    earlier = [b for b in books if latest["ts"] - b["ts"] <= window_s]
    if len(earlier) < 2:
        return None
    first, last = resting_size(earlier[0], price), resting_size(latest, price)
    size0 = first["size"] if first else 0
    size1 = last["size"] if last else 0
    if size0 <= 0 or size1 < add_mult * size0:
        return None
    return {"price": price, "was": size0, "now": size1}


def iceberg(book: dict, prints: Sequence[tuple], price: float, fill_mult: float, window_s: float) -> dict[str, Any] | None:
    """A5.5 — prints at a level exceed displayed size by fill_mult× while the
    display stays put: a hidden refreshing order. Real absorption."""
    shown = resting_size(book, price)
    if not shown or shown["size"] <= 0:
        return None
    t1 = book["ts"]
    traded = _traded_at(prints, price, t1 - window_s, t1)
    if traded < fill_mult * shown["size"]:
        return None
    return {"price": price, "displayed": shown["size"], "traded_through": traded, "side": shown["side"]}


def spoof_events(
    books: Sequence[dict], prints: Sequence[tuple], min_size: float, approach_ticks: int
) -> list[dict[str, Any]]:
    """A5.6 — large size that appears, never fills, and vanishes as price
    approaches within approach_ticks. Each hit discounts 5.1–5.4."""
    out: list[dict[str, Any]] = []
    for i in range(1, len(books)):
        prev, cur = books[i - 1], books[i]
        last_price = _last_trade_price(prints, cur["ts"])
        if last_price is None:
            continue
        for side in ("bids", "asks"):
            cur_prices = {p for p, _ in cur.get(side) or []}
            for p, s in prev.get(side) or []:
                if s < min_size or p in cur_prices:
                    continue
                if abs(last_price - p) > approach_ticks * TICK:
                    continue
                filled = _traded_at(prints, p, prev["ts"], cur["ts"])
                if filled < s * 0.25:
                    out.append({"price": p, "size": s, "side": side[:-1], "ts": cur["ts"]})
    return out


def depth_vacuum(book: dict, last: float, band_ticks: int, session_totals: Sequence[float], pctile: float) -> dict[str, Any] | None:
    """A5.7 — total displayed size within band_ticks of last below the
    session's Pxx: thin book, fast moves, bad fills."""
    band = band_ticks * TICK
    total = sum(
        s for side in ("bids", "asks") for p, s in book.get(side) or [] if abs(p - last) <= band
    )
    if not session_totals:
        return None
    s = sorted(session_totals)
    threshold = s[max(0, min(len(s) - 1, int(len(s) * pctile / 100.0)))]
    if total >= threshold:
        return None
    return {"total_in_band": total, f"session_p{pctile:.0f}": threshold, "read": "thin book near price"}


def _last_trade_price(prints: Sequence[tuple], before_ts: float) -> float | None:
    past = [(ts, p) for ts, p, _s, _d in prints if ts <= before_ts]
    return max(past)[1] if past else None
