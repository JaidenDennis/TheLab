"""A2 — footprint: per-price aggressor volume inside a bar.

`cells` is {price: {"buy","sell","max_print"}} — "buy" is aggressive buying
at that price (lifted the offer), "sell" is aggressive selling (hit the bid).
Built by merging the engine's per-minute VBP hashes over a bar's span."""

from __future__ import annotations

from typing import Any


def merge_cells(minutes: list[dict[float, dict[str, float]]]) -> dict[float, dict[str, float]]:
    """Merge per-minute footprints into one bar footprint."""
    out: dict[float, dict[str, float]] = {}
    for m in minutes:
        for price, c in m.items():
            cell = out.setdefault(price, {"buy": 0.0, "sell": 0.0, "max_print": 0.0})
            cell["buy"] += c.get("buy", 0.0)
            cell["sell"] += c.get("sell", 0.0)
            cell["max_print"] = max(cell["max_print"], c.get("max_print", 0.0))
    return out


def bar_poc(cells: dict[float, dict[str, float]]) -> float | None:
    """A2.4 — price with max total volume in the bar."""
    if not cells:
        return None
    return max(sorted(cells), key=lambda p: cells[p]["buy"] + cells[p]["sell"])


def poc_position(poc: float | None, high: float, low: float) -> str | None:
    """Where the POC sits within the bar: top / mid / low third."""
    if poc is None or high <= low:
        return None
    frac = (poc - low) / (high - low)
    return "top" if frac >= 2 / 3 else "low" if frac <= 1 / 3 else "mid"


def poc_migration(pocs: list[float | None]) -> str | None:
    """A2.5 — direction of the last POC step: rising / falling / flat."""
    known = [p for p in pocs if p is not None]
    if len(known) < 2:
        return None
    if known[-1] > known[-2]:
        return "rising"
    if known[-1] < known[-2]:
        return "falling"
    return "flat"


def diagonal_imbalances(cells: dict[float, dict[str, float]], ratio: float, tick: float) -> list[dict[str, Any]]:
    """A2.2 — buy imbalance when ask volume at p overwhelms bid volume at
    p − 1 tick by `ratio`; sell imbalance on the inverse diagonal."""
    out: list[dict[str, Any]] = []
    for p in sorted(cells):
        below = cells.get(round(p - tick, 10))
        if below is not None:
            buy_here = cells[p]["buy"]
            sell_below = below["sell"]
            if buy_here >= ratio * max(sell_below, 1.0):
                out.append({"price": p, "side": "buy", "ratio": round(buy_here / max(sell_below, 1.0), 1)})
            elif sell_below >= ratio * max(buy_here, 1.0):
                out.append({"price": round(p - tick, 10), "side": "sell", "ratio": round(sell_below / max(buy_here, 1.0), 1)})
    return out


def stacked_imbalances(imbalances: list[dict[str, Any]], min_run: int, tick: float) -> list[dict[str, Any]]:
    """A2.3 — ≥ min_run consecutive (adjacent-price) imbalances, same side."""
    stacks: list[dict[str, Any]] = []
    by_side: dict[str, list[float]] = {"buy": [], "sell": []}
    for im in imbalances:
        by_side[im["side"]].append(im["price"])
    for side, prices in by_side.items():
        prices = sorted(set(prices))
        run: list[float] = []
        for p in prices:
            if run and abs(p - run[-1] - tick) < 1e-9:
                run.append(p)
            else:
                if len(run) >= min_run:
                    stacks.append({"side": side, "low": run[0], "high": run[-1], "n": len(run)})
                run = [p]
        if len(run) >= min_run:
            stacks.append({"side": side, "low": run[0], "high": run[-1], "n": len(run)})
    return stacks


def auction_state(cells: dict[float, dict[str, float]]) -> dict[str, str]:
    """A2.6/2.7 — at each extreme: 'finished' when the aggressor side printed
    zero there (clean give-up), else 'unfinished' (both sides traded — the
    auction didn't complete; price tends to revisit)."""
    if not cells:
        return {"high": "unknown", "low": "unknown"}
    hi, lo = max(cells), min(cells)
    return {
        "high": "finished" if cells[hi]["buy"] == 0 else "unfinished",
        "low": "finished" if cells[lo]["sell"] == 0 else "unfinished",
    }


def hvn_lvn(cells: dict[float, dict[str, float]], hvn_mult: float, lvn_mult: float) -> dict[str, list[float]]:
    """A2.8/2.9 — levels with volume > hvn_mult× bar mean / < lvn_mult× mean."""
    if not cells:
        return {"hvn": [], "lvn": []}
    totals = {p: c["buy"] + c["sell"] for p, c in cells.items()}
    mean = sum(totals.values()) / len(totals)
    return {
        "hvn": sorted(p for p, v in totals.items() if v > hvn_mult * mean),
        "lvn": sorted(p for p, v in totals.items() if v < lvn_mult * mean),
    }


def delta_concentration(cells: dict[float, dict[str, float]]) -> str | None:
    """A2.10 — which third of the bar's range holds most of the |delta|."""
    if not cells:
        return None
    hi, lo = max(cells), min(cells)
    if hi <= lo:
        return "mid"
    thirds = [0.0, 0.0, 0.0]
    for p, c in cells.items():
        frac = (p - lo) / (hi - lo)
        idx = 2 if frac >= 2 / 3 else 0 if frac <= 1 / 3 else 1
        thirds[idx] += abs(c["buy"] - c["sell"])
    return ("bottom", "mid", "top")[max(range(3), key=lambda i: thirds[i])]
