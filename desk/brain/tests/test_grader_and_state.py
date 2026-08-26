"""Grader race logic, value area, price bins, levels statuses, qrank, watcher rate limit."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from desk_brain.grader import _brier, _race
from desk_brain.state.levels import LevelBook, _status
from desk_brain.state.pricebins import PriceBinAggregator, value_area
from desk_brain.state.qrank import QRank
from desk_brain.watcher import Watcher

from nq_agent.models import Bar, Tick


def bar(hi: float, lo: float) -> SimpleNamespace:
    return SimpleNamespace(high=hi, low=lo)


class TestRace:
    def test_favorable_first_hits(self):
        assert _race([bar(20105, 20099), bar(20112, 20101)], 20100, +1) is True

    def test_adverse_first_misses(self):
        assert _race([bar(20101, 20089), bar(20120, 20095)], 20100, +1) is False

    def test_ambiguous_bar_is_adverse_first(self):
        # both 10pt thresholds inside one bar: house convention says adverse wins
        assert _race([bar(20111, 20089)], 20100, +1) is False

    def test_short_direction(self):
        assert _race([bar(20104, 20088)], 20100, -1) is True

    def test_no_resolution_is_miss(self):
        assert _race([bar(20103, 20098)], 20100, +1) is False


def test_brier():
    assert _brier(0.8, True) == round((0.8 - 1) ** 2, 4)
    assert _brier(0.8, False) == round(0.8**2, 4)
    assert _brier(None, True) is None


def test_value_area_covers_70_pct_around_poc():
    profile = {100.0: 10, 100.25: 50, 100.5: 100, 100.75: 40, 101.0: 10}
    va = value_area(profile)
    assert va["poc"] == 100.5
    assert va["val"] <= 100.5 <= va["vah"]
    total = sum(profile.values())
    covered = sum(v for p, v in profile.items() if va["val"] <= p <= va["vah"])
    assert covered >= 0.7 * total


def make_tick(price: str, size: int, side: str | None, ts: datetime) -> Tick:
    return Tick(symbol="NQ", ts=ts, price=Decimal(price), size=size, side=side)


def test_pricebins_aggressor_and_tick_rule():
    agg = PriceBinAggregator()
    t0 = datetime(2026, 8, 24, 14, 0, 5, tzinfo=timezone.utc)
    agg.on_tick(make_tick("20100.00", 3, "B", t0))
    agg.on_tick(make_tick("20100.00", 2, "A", t0))
    agg.on_tick(make_tick("20100.25", 4, None, t0))  # uptick -> buy
    minute = int(t0.timestamp() // 60)
    bins = agg.minutes[minute]
    assert bins[20100.0] == [3.0, 2.0, 3.0]
    assert bins[20100.25][0] == 4.0

    drained = agg.drain_before(minute + 1)
    assert minute in drained and minute not in agg.minutes
    packed = PriceBinAggregator.pack(drained[minute])
    assert packed["20100.0"] == "3,2,3"


def test_level_status_rules():
    # untested: session never near it
    assert _status(20200.0, last=20100.0, hi=20120.0, lo=20080.0) == "untested"
    # swept: session range crossed it
    assert _status(20100.0, last=20090.0, hi=20120.0, lo=20080.0) == "swept"
    # defending: touched within 3 pts, not crossed
    assert _status(20122.0, last=20110.0, hi=20120.0, lo=20080.0) == "defending"


def _mk_bar(day: int, hour: int, minute: int, o: float, h: float, lo: float, c: float, vol: int = 100) -> Bar:
    # ET times passed as UTC-4 (August, EDT)
    ts = datetime(2026, 8, day, hour + 4, minute, tzinfo=timezone.utc)
    return Bar(symbol="NQ", timeframe="1m", open_time=ts, open=Decimal(str(o)), high=Decimal(str(h)),
               low=Decimal(str(lo)), close=Decimal(str(c)), volume=vol)


def test_levelbook_builds_pdh_pdl_and_on_range():
    from datetime import date

    bars = [
        _mk_bar(21, 10, 0, 20000, 20120, 19980, 20100),  # prior day RTH (Fri 08/21)
        _mk_bar(21, 15, 59, 20100, 20110, 20060, 20080),  # prior close 20080
        _mk_bar(21, 19, 0, 20080, 20095, 20040, 20050),  # overnight (18:00+ ET prior day)
        _mk_bar(24, 5, 0, 20050, 20070, 20030, 20060),  # overnight morning of session
    ]
    book = LevelBook.build(bars, date(2026, 8, 24))
    names = {lv.name: lv.price for lv in book.levels}
    assert names["PDH"] == 20120.0
    assert names["PDL"] == 19980.0
    assert book.prior_close == 20080.0
    assert names["ON-H"] == 20095.0
    assert names["ON-L"] == 20030.0
    assert book.prior_value_area["poc"] is not None


def test_qrank_percentile():
    q = QRank([0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10])
    assert q.rank(0.10) == 100.0
    assert q.rank(-0.05) == 50.0  # abs value
    assert q.rank(0.001) == 0.0
    assert q.rank(None) is None
    assert QRank([]).rank(0.5) is None


def test_watcher_rate_limit_per_trigger_and_global():
    w = Watcher.__new__(Watcher)  # no ctx needed for _rate_ok
    w._last_fired = {}
    w._session_count = 0
    w._suppressed = 0
    assert w._rate_ok("bias_flip") is True
    w._last_fired["bias_flip"] = datetime.now(timezone.utc)
    w._session_count = 1
    assert w._rate_ok("bias_flip") is False  # cooldown
    w._last_fired["bias_flip"] = datetime.now(timezone.utc) - timedelta(seconds=601)
    assert w._rate_ok("bias_flip") is True  # cooldown expired
    w._session_count = 8
    assert w._rate_ok("stop_proximity") is False  # global cap
    assert w._suppressed == 1
    assert w._rate_ok("close_summary") is True  # unlimited triggers exempt
