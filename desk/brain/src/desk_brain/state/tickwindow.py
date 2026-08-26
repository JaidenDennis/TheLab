"""Rolling tick window + session tape statistics.

The engine keeps a few minutes of raw prints in memory (ticks don't persist
anywhere else) and samples session-scale distributions once per second so the
tape signals can percentile-rank against "today" without storing every print.
Same aggressor convention as MinuteFlowAggregator (tick-rule fallback)."""

from __future__ import annotations

from collections import Counter, deque
from datetime import timezone
from typing import Any

from nq_agent.models import Tick

WINDOW_S = 600.0  # raw prints retained
SPEED_SAMPLES_MAX = 7200  # 1/s samples, ~2h of session distribution
ABSORPTION_SCORES_MAX = 500


class TickWindow:
    def __init__(self) -> None:
        self.ticks: deque[tuple[float, float, int, int]] = deque()  # (ts_s, price, size, dir)
        self._last_price: float | None = None
        self._last_dir: int = 0
        self.size_counts: Counter[int] = Counter()  # session print-size histogram
        self.speed_samples: deque[float] = deque(maxlen=SPEED_SAMPLES_MAX)
        self.absorption_scores: deque[float] = deque(maxlen=ABSORPTION_SCORES_MAX)

    # -- hot path -----------------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        price = float(tick.price)
        size = int(tick.size)
        if tick.side == "B":
            direction = 1
        elif tick.side == "A":
            direction = -1
        else:
            if self._last_price is None or price == self._last_price:
                direction = self._last_dir or 1
            else:
                direction = 1 if price > self._last_price else -1
        self._last_price = price
        self._last_dir = direction
        ts = tick.ts.astimezone(timezone.utc).timestamp()
        self.ticks.append((ts, price, size, direction))
        self.size_counts[size] += 1
        while self.ticks and ts - self.ticks[0][0] > WINDOW_S:
            self.ticks.popleft()

    # -- 1s sampling --------------------------------------------------------

    def sample_speed(self, speed: float) -> None:
        self.speed_samples.append(speed)

    def note_absorption(self, raw_score: float) -> None:
        self.absorption_scores.append(raw_score)

    # -- session stats ------------------------------------------------------

    def session_median_print(self) -> float | None:
        total = sum(self.size_counts.values())
        if total == 0:
            return None
        half = total / 2.0
        acc = 0
        for size in sorted(self.size_counts):
            acc += self.size_counts[size]
            if acc >= half:
                return float(size)
        return None

    def snapshot(self) -> list[tuple[float, float, int, int]]:
        return list(self.ticks)

    def stats(self) -> dict[str, Any]:
        return {
            "prints_in_window": len(self.ticks),
            "session_median_print": self.session_median_print(),
        }
