"""Per-price aggressor volume by minute — the footprint's raw material.

Structural twin of nq_agent.flow.MinuteFlowAggregator (same on_tick contract,
same aggressor rule) but binned by price instead of trade size. Net-new for
desk-brain: nothing in the engine aggregates by price.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timezone
from typing import Any

from nq_agent.models import Tick


class PriceBinAggregator:
    """minute (epoch // 60, UTC) -> price -> [buy_vol, sell_vol, max_single_print]."""

    def __init__(self) -> None:
        self.minutes: dict[int, dict[float, list[float]]] = defaultdict(dict)
        self._last_price: float | None = None
        self._last_dir: int = 0

    def on_tick(self, tick: Tick) -> None:
        price = float(tick.price)
        size = int(tick.size)
        side = tick.side

        if side == "B":
            direction = 1
        elif side == "A":
            direction = -1
        else:
            # tick rule fallback, same convention as MinuteFlowAggregator
            if self._last_price is None or price == self._last_price:
                direction = self._last_dir
            else:
                direction = 1 if price > self._last_price else -1
        self._last_price = price
        if direction != 0:
            self._last_dir = direction

        minute = int(tick.ts.astimezone(timezone.utc).timestamp() // 60)
        cell = self.minutes[minute].setdefault(price, [0.0, 0.0, 0.0])
        if direction >= 0:
            cell[0] += size
        else:
            cell[1] += size
        cell[2] = max(cell[2], float(size))

    def drain_before(self, minute: int) -> dict[int, dict[float, list[float]]]:
        """Remove and return all fully-elapsed minutes (< minute) for persistence."""
        done = {m: bins for m, bins in self.minutes.items() if m < minute}
        for m in done:
            del self.minutes[m]
        return done

    def snapshot(self) -> dict[int, dict[float, list[float]]]:
        return {m: dict(bins) for m, bins in self.minutes.items()}

    @staticmethod
    def pack(bins: dict[float, list[float]]) -> dict[str, str]:
        """Redis hash mapping: price -> 'buy,sell,max_print'."""
        return {str(p): f"{v[0]:g},{v[1]:g},{v[2]:g}" for p, v in bins.items()}


def value_area(profile: dict[float, float], pct: float = 0.70) -> dict[str, float | None]:
    """POC + value area covering `pct` of volume, expanding greedily from POC."""
    if not profile:
        return {"vah": None, "poc": None, "val": None}
    prices = sorted(profile)
    poc = max(prices, key=lambda p: (profile[p], -abs(p)))
    total = sum(profile.values())
    target = total * pct
    i = j = prices.index(poc)
    acc = profile[poc]
    while acc < target and (i > 0 or j < len(prices) - 1):
        below = profile[prices[i - 1]] if i > 0 else -1.0
        above = profile[prices[j + 1]] if j < len(prices) - 1 else -1.0
        if above >= below:
            j += 1
            acc += profile[prices[j]]
        else:
            i -= 1
            acc += profile[prices[i]]
    return {"vah": prices[j], "poc": poc, "val": prices[i]}
