"""Tick-flow feature computation -- ONE implementation for research and live.

MinuteFlowAggregator turns a tick stream into per-minute aggressor-signed
aggregates; FlowEngine turns those into the per-5m-bar decision records the
TFR strategy consumes, using a calibration (percentile table, volume
z-stats) computed nightly from trailing sessions.

The research pipeline (scripts/precompute_flow.py, scripts/fit_regimes.py,
scripts/calibrate_flow.py) imports these same classes; the shadow harness
feeds them live ticks. If the two ever disagree, that is a bug in exactly
one place, and the drift audit exists to prove they do not.

Zero lookahead by construction: a minute's aggregate is complete only when
a tick at/after the next minute boundary arrives (the same rule as
BarAggregator), and a 5m decision record is written only from completed
minutes. The engine's tick tap runs before bar aggregation, so by the time
a closed 5m Bar reaches the strategy, its decision record already exists.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from nq_agent.models import Tick

ET = ZoneInfo("America/New_York")
SIZE_CUTS = (3, 5, 10, 20)
RTH_LAST_INDEX = 390  # the minute ending 16:00 ET


def minute_index(ts_et: datetime) -> int:
    """1 == the minute ending 09:31 ET ... 390 == the minute ending 16:00.

    A tick belongs to the minute it will CLOSE: a tick at 09:30:xx is part
    of the bucket whose bar closes at 09:31, index 1.
    """
    return (ts_et.hour - 9) * 60 + ts_et.minute - 30 + 1


class MinuteFlowAggregator:
    """Per-minute RTH flow aggregates from a tick stream, with QA.

    Databento side convention: 'B' = buy aggressor, 'A' = sell aggressor,
    'N'/missing = unknown, resolved by the tick rule (uptick buy, downtick
    sell, unchanged carries). The tick-rule agreement statistic exists to
    catch an inverted convention loudly.
    """

    def __init__(self) -> None:
        self.minutes: dict[int, dict[str, Any]] = {}
        self._sizes: list[int] = []
        self._unknown = 0
        self._total = 0
        self._rule_hits = 0
        self._rule_checked = 0
        self._prev_price: Decimal | None = None
        self._last_direction = 0

    def on_tick(self, tick: Tick) -> None:
        ts_et = tick.ts.astimezone(ET)
        index = minute_index(ts_et)
        if not (1 <= index <= RTH_LAST_INDEX):
            return
        self._total += 1
        self._sizes.append(tick.size)

        if self._prev_price is not None:
            if tick.price > self._prev_price:
                rule = 1
            elif tick.price < self._prev_price:
                rule = -1
            else:
                rule = self._last_direction
        else:
            rule = 0
        if rule != 0:
            self._last_direction = rule
        self._prev_price = tick.price

        side = tick.side or "N"
        if side == "B":
            direction = 1
        elif side == "A":
            direction = -1
        else:
            self._unknown += 1
            direction = rule
        if side in ("B", "A") and rule != 0:
            self._rule_checked += 1
            self._rule_hits += int((1 if side == "B" else -1) == rule)

        bucket = self.minutes.setdefault(
            index,
            {
                "close": None,
                "vol": 0,
                "buy": 0,
                "sell": 0,
                **{f"buy_ge{c}": 0 for c in SIZE_CUTS},
                **{f"sell_ge{c}": 0 for c in SIZE_CUTS},
            },
        )
        bucket["close"] = str(tick.price)
        bucket["vol"] += tick.size
        if direction != 0:
            key = "buy" if direction > 0 else "sell"
            bucket[key] += tick.size
            for cut in SIZE_CUTS:
                if tick.size >= cut:
                    bucket[f"{key}_ge{cut}"] += tick.size

    def qa(self) -> dict[str, Any]:
        sizes = sorted(self._sizes)

        def pct(p: float) -> int:
            return sizes[min(len(sizes) - 1, int(len(sizes) * p))] if sizes else 0

        unknown_share = self._unknown / self._total if self._total else 1.0
        agreement = self._rule_hits / self._rule_checked if self._rule_checked else 0.0
        return {
            "ticks": self._total,
            "unknown_share": round(unknown_share, 5),
            "tick_rule_agreement": round(agreement, 4),
            "size_p50": pct(0.50),
            "size_p90": pct(0.90),
            "size_p95": pct(0.95),
            "size_p99": pct(0.99),
            "excluded": unknown_share > 0.05,
        }

    def session_payload(self) -> dict[str, Any]:
        """The pass-1 flow-file shape (precompute_flow's output), for
        persistence and the offline drift recomputation."""
        return {"qa": self.qa(), "minutes": {str(k): v for k, v in sorted(self.minutes.items())}}


def flow_over(minutes: dict[int, dict[str, Any]], end: int, span: int) -> float:
    """F1 over the trailing `span` minutes ending at minute index `end`."""
    buy = sell = vol = 0
    for i in range(max(1, end - span + 1), end + 1):
        m = minutes.get(i)
        if m is None:
            continue
        buy += m["buy"]
        sell += m["sell"]
        vol += m["vol"]
    if vol == 0:
        return 0.0
    return (buy - sell) / vol


def percentile(samples: list[float], pct: int) -> float:
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * pct // 100))
    return ordered[rank - 1]


class FlowEngine:
    """Live decision records for TFR v1.1, from the shared aggregator.

    Fed ticks via `on_tick` (the feed's tick tap). On each completed 5m
    boundary it writes a decision record into `book`, a mapping the
    TickFlowRegime strategy reads directly -- the live counterpart of a
    fit_regimes decision file. Regime fields are absent (annotations were
    demoted in cycle 1); the strategy's invariant test guarantees they
    cannot influence trading anyway.

    `calibration` carries what walk-forward research supplied per session:
      q_f1     -- percentile table of trailing-60-session |F1_5|
      vol_mean -- mean of trailing 5m volumes
      vol_sd   -- their standard deviation (population)
    Produced by scripts/calibrate_flow.py from persisted flow files, never
    from the current session (zero lookahead, unchanged).
    """

    def __init__(self, calibration: dict[str, Any], book: dict[str, Any]) -> None:
        self._calibration = calibration
        self._book = book
        self._aggregator = MinuteFlowAggregator()
        self._session: str | None = None
        self._last_emitted = 0

    @property
    def aggregator(self) -> MinuteFlowAggregator:
        return self._aggregator

    def on_tick(self, tick: Tick) -> None:
        ts_et = tick.ts.astimezone(ET)
        session = ts_et.date().isoformat()
        if session != self._session:
            self._session = session
            self._aggregator = MinuteFlowAggregator()
            self._last_emitted = 0
            self._book[session] = {
                "model": "live",
                "mahal_cut": None,
                "q_f1": self._calibration.get("q_f1"),
                "size_cut": self._calibration.get("size_cut", 5),
                "bars": {},
            }

        # A tick in minute-index N means every 5m boundary <= N-1 is now
        # complete; emit any decision bars not yet written.
        index = minute_index(ts_et)
        boundary = ((index - 1) // 5) * 5  # last complete 5m boundary
        while self._last_emitted < boundary and self._last_emitted < RTH_LAST_INDEX:
            self._last_emitted += 5
            self._emit(self._last_emitted)

        self._aggregator.on_tick(tick)

    def finish_session(self) -> None:
        """Emit the final boundary at RTH end (called at/after 16:00 ET)."""
        while self._last_emitted < RTH_LAST_INDEX:
            self._last_emitted += 5
            self._emit(self._last_emitted)

    def _emit(self, end: int) -> None:
        minutes = self._aggregator.minutes
        window = [minutes[i] for i in range(end - 4, end + 1) if i in minutes]
        if not window or self._session is None:
            return
        vol_5m = sum(m["vol"] for m in window)
        f1_5 = flow_over(minutes, end, 5)
        mean = float(self._calibration.get("vol_mean", 0.0))
        sd = float(self._calibration.get("vol_sd", 0.0))
        z_vol = 0.0 if sd == 0 else (vol_5m - mean) / sd
        self._book[self._session]["bars"][str(end)] = {
            "close": float(window[-1]["close"]),
            "f1_2": flow_over(minutes, end, 2),
            "f1_5": f1_5,
            "f1_15": flow_over(minutes, end, 15),
            "z_vol": round(z_vol, 3),
            "regime": None,
            "t_af": None,
            "mahal": None,
        }
