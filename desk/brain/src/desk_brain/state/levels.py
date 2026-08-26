"""Session levels — net-new for desk-brain (no engine module computes these).

Bootstrapped once at startup from historical 1m bars, then statuses are
recomputed intraday against session extremes and last price. Value areas use
the standard bar-range volume approximation for the *prior* day (no tick data
retroactively); the *developing* area comes from live tick-true price bins.

ON-high/low convention: 18:00 ET prior calendar day through 09:30 today —
same catalog as SFB (sfb_study.py), including its Sunday-evening caveat.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from nq_agent.models import Bar

from .pricebins import value_area

ET = ZoneInfo("America/New_York")
TICK = 0.25
TOUCH_PTS = 3.0  # within this = tested


@dataclass
class Level:
    name: str
    price: float
    kind: str  # pdh/pdl/pwh/pwl/on_h/on_l/swing_h/swing_l/gamma_wall
    multi_touch: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class LevelBook:
    session_date: date
    levels: list[Level] = field(default_factory=list)
    prior_close: float | None = None
    prior_value_area: dict[str, float | None] = field(default_factory=dict)
    on_high: float | None = None
    on_low: float | None = None

    @classmethod
    def build(cls, bars_1m: Iterable[Bar], session_date: date) -> "LevelBook":
        book = cls(session_date=session_date)
        by_day: dict[date, list[Bar]] = defaultdict(list)
        for b in bars_1m:
            by_day[b.open_time.astimezone(ET).date()].append(b)
        if not by_day:
            return book

        prior_days = sorted(d for d in by_day if d < session_date and by_day[d])
        if not prior_days:
            return book
        prior = prior_days[-1]

        def rth(bars: list[Bar]) -> list[Bar]:
            return [b for b in bars if time(9, 30) <= b.open_time.astimezone(ET).time() < time(16, 0)]

        prior_rth = rth(by_day[prior])
        if prior_rth:
            book.levels.append(Level("PDH", float(max(b.high for b in prior_rth)), "pdh"))
            book.levels.append(Level("PDL", float(min(b.low for b in prior_rth)), "pdl"))
            book.prior_close = float(prior_rth[-1].close)
            book.prior_value_area = _bar_value_area(prior_rth)

        # Prior ISO week RTH extremes
        this_week = session_date.isocalendar()[:2]
        prev_week_days = [d for d in prior_days if d.isocalendar()[:2] != this_week]
        if prev_week_days:
            last_wk = prev_week_days[-1].isocalendar()[:2]
            wk_bars = [b for d in prev_week_days if d.isocalendar()[:2] == last_wk for b in rth(by_day[d])]
            if wk_bars:
                book.levels.append(Level("PWH", float(max(b.high for b in wk_bars)), "pwh"))
                book.levels.append(Level("PWL", float(min(b.low for b in wk_bars)), "pwl"))

        # Overnight: prior day 18:00 ET -> today 09:30 ET
        on_start = datetime.combine(prior, time(18, 0), tzinfo=ET)
        on_end = datetime.combine(session_date, time(9, 30), tzinfo=ET)
        on_bars = [
            b
            for d in (prior, session_date)
            for b in by_day.get(d, [])
            if on_start <= b.open_time.astimezone(ET) < on_end
        ]
        if on_bars:
            book.on_high = float(max(b.high for b in on_bars))
            book.on_low = float(min(b.low for b in on_bars))
            book.levels.append(Level("ON-H", book.on_high, "on_h"))
            book.levels.append(Level("ON-L", book.on_low, "on_l"))

        book.levels.extend(_swings_4h(by_day, session_date))
        return book

    def add_gamma_walls(self, walls: list[dict[str, Any]]) -> None:
        for w in walls:
            self.levels.append(
                Level(f"γ-wall {w['strike']:g}", float(w["strike"]), "gamma_wall", meta={"gex": w.get("gex"), "side": w.get("side")})
            )

    def snapshot(self, last: float | None, session_high: float | None, session_low: float | None) -> dict[str, Any]:
        out = []
        for lv in sorted(self.levels, key=lambda x: -x.price):
            out.append(
                {
                    "name": lv.name,
                    "kind": lv.kind,
                    "price": round(lv.price, 2),
                    "multi_touch": lv.multi_touch,
                    "distance_pts": round(abs(last - lv.price), 2) if last is not None else None,
                    "status": _status(lv.price, last, session_high, session_low),
                    **({"meta": lv.meta} if lv.meta else {}),
                }
            )
        return {"levels": out, "prior_close": self.prior_close}


def _status(level: float, last: float | None, hi: float | None, lo: float | None) -> str:
    if last is None or hi is None or lo is None:
        return "untested"
    touched = (lo - TOUCH_PTS) <= level <= (hi + TOUCH_PTS)
    if not touched:
        return "untested"
    crossed = lo < level < hi
    if crossed:
        return "swept"
    return "defending"


def _bar_value_area(bars: list[Bar]) -> dict[str, float | None]:
    """Volume-at-price approximation: each bar's volume spread across its range."""
    profile: dict[float, float] = defaultdict(float)
    for b in bars:
        lo, hi = float(b.low), float(b.high)
        n_ticks = max(1, int(round((hi - lo) / TICK)) + 1)
        share = b.volume / n_ticks
        for i in range(n_ticks):
            profile[round(lo + i * TICK, 2)] += share
    return value_area(dict(profile))


def _swings_4h(by_day: dict[date, list[Bar]], session_date: date) -> list[Level]:
    """Globex-aligned 4H swing highs/lows over the trailing days, multi-touch flagged."""
    candles: list[tuple[datetime, float, float]] = []  # (start, high, low)
    all_bars = sorted(
        (b for d, bars in by_day.items() if d <= session_date for b in bars),
        key=lambda b: b.open_time,
    )
    if not all_bars:
        return []
    bucket: dict[datetime, tuple[float, float]] = {}
    for b in all_bars:
        et = b.open_time.astimezone(ET)
        # anchor 4H blocks at 18:00 ET
        hours_since = (et.hour - 18) % 24
        start = (et - timedelta(hours=hours_since % 4, minutes=et.minute)).replace(second=0, microsecond=0)
        hi, lo = bucket.get(start, (float("-inf"), float("inf")))
        bucket[start] = (max(hi, float(b.high)), min(lo, float(b.low)))
    ordered = sorted(bucket.items())
    swings: list[Level] = []
    for i in range(1, len(ordered) - 1):
        _, (hi, lo) = ordered[i]
        (_, (ph, pl)), (_, (nh, nl)) = ordered[i - 1], ordered[i + 1]
        if hi > ph and hi > nh:
            swings.append(Level(f"4H-swing-H {hi:g}", hi, "swing_h"))
        if lo < pl and lo < nl:
            swings.append(Level(f"4H-swing-L {lo:g}", lo, "swing_l"))
    # multi-touch: another swing of the same side within 5 pts
    for s in swings:
        s.multi_touch = any(o is not s and o.kind == s.kind and abs(o.price - s.price) <= 5 for o in swings)
    # dedupe multi-touch clusters to their most recent representative
    seen: list[Level] = []
    for s in reversed(swings):
        if not any(o.kind == s.kind and abs(o.price - s.price) <= 5 for o in seen):
            seen.append(s)
    return list(reversed(seen))[-8:]
