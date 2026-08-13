"""NAIM driven through the engine-faithful harness (same Driver as SME's
tests: real tracker, real context, same bar ordering).

Curve fixture: a flat 0.1% sigma at every minute, open 20000, prev close
20000 -- so UB is 20020 and LB is 19980 all day. July 2026 is EDT: 09:30 ET
== 13:30 UTC.
"""

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.position import PositionTracker
from nq_agent.strategy.naim import ACTIVE, DONE, IN_TRADE, NoiseAreaIntradayMomentum

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
SESSION = date(2026, 7, 15)
CAL = SessionCalendar("America/New_York", time(9, 30), time(16, 30))

FLAT_CURVE = {
    SESSION.isoformat(): {
        "open": "20000.00",
        "prev_close": "20000.00",
        "sigma": {str(i): 0.001 for i in range(1, 391)},
    }
}


def bar_1m(minute: int, high: str, low: str, close: str, open_: str | None = None) -> Bar:
    return Bar(
        symbol="NQ",
        timeframe="1m",
        open_time=OPEN + timedelta(minutes=minute),
        open=Decimal(open_ if open_ is not None else low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=100,
    )


class Driver:
    def __init__(self, strategy: NoiseAreaIntradayMomentum) -> None:
        self.strategy = strategy
        self.tracker = PositionTracker()
        self.clock = SimClock(OPEN - timedelta(hours=1))
        self.ctx = Context(self.clock, CAL, 500)
        self.trades = 0
        self.signals: list[Signal] = []

    def feed(self, bars: list[Bar]) -> list[Signal]:
        emitted = []
        for bar in bars:
            self.clock.advance_to(bar.close_time)
            self.tracker.on_bar(bar)
            self.ctx.record_bar(bar)
            self.ctx.set_position(self.tracker.position)
            self.ctx.set_trades_taken(self.trades)
            signal = self.strategy.on_bar(bar, self.ctx)
            if signal is None:
                continue
            emitted.append(signal)
            self.signals.append(signal)
            if signal.intent is SignalIntent.ENTRY:
                if self.tracker.on_signal(signal) is not None:
                    self.trades += 1
            else:
                self.tracker.flatten(bar.close, bar.close_time)
        return emitted


def naim(**kwargs: object) -> NoiseAreaIntradayMomentum:
    kwargs.setdefault("noise_curves", FLAT_CURVE)
    return NoiseAreaIntradayMomentum(**kwargs)  # type: ignore[arg-type]


def opening(minutes: int = 5, close: str = "20000") -> list[Bar]:
    """Bars that stay inside the 19980..20020 band."""
    return [
        bar_1m(m, high="20010", low="19990", close=close, open_="20000")
        for m in range(minutes)
    ]


def entries(signals: list[Signal]) -> list[Signal]:
    return [s for s in signals if s.intent is SignalIntent.ENTRY]


# --- the band ---------------------------------------------------------------


def test_no_curve_means_no_trades() -> None:
    driver = Driver(NoiseAreaIntradayMomentum(noise_curves={}))
    breakout = bar_1m(5, high="20030", low="20015", close="20028", open_="20015")

    assert driver.feed(opening() + [breakout]) == []
    assert driver.strategy._state == DONE


def test_close_above_the_band_goes_long() -> None:
    driver = Driver(naim())
    breakout = bar_1m(5, high="20030", low="20015", close="20028", open_="20015")

    signals = driver.feed(opening() + [breakout])

    entry = entries(signals)[0]
    assert entry.direction is Direction.LONG
    assert entry.entry_price == Decimal("20028.25")  # close + 1 tick adverse
    assert entry.stop_price == Decimal("19948.25")  # catastrophic, entry - 80
    assert entry.target_price == Decimal("21028.25")  # far target


def test_close_below_the_band_goes_short() -> None:
    driver = Driver(naim())
    breakdown = bar_1m(5, high="19985", low="19970", close="19972", open_="19985")

    signals = driver.feed(opening() + [breakdown])

    assert entries(signals)[0].direction is Direction.SHORT


def test_a_touch_without_a_close_beyond_does_not_trigger() -> None:
    driver = Driver(naim())
    wick = bar_1m(5, high="20035", low="20005", close="20012", open_="20005")

    assert driver.feed(opening() + [wick]) == []


def test_gap_anchor_widens_the_band() -> None:
    """Open 20000 vs prev close 20100: the upper anchor is the prev close,
    so a move to 20120 is still inside 20100*(1+0.001)=20120.10."""
    curve = {
        SESSION.isoformat(): {
            "open": "20000.00",
            "prev_close": "20100.00",
            "sigma": {str(i): 0.001 for i in range(1, 391)},
        }
    }
    driver = Driver(NoiseAreaIntradayMomentum(noise_curves=curve))
    probe = bar_1m(5, high="20120", low="20100", close="20120", open_="20100")

    assert driver.feed(opening() + [probe]) == []


def test_30m_mode_ignores_off_boundary_triggers() -> None:
    driver = Driver(naim(trigger_mode="30m"))
    breakout = bar_1m(5, high="20030", low="20015", close="20028", open_="20015")  # 09:36
    boundary = bar_1m(29, high="20030", low="20015", close="20028", open_="20015")  # 10:00

    assert driver.feed(opening() + [breakout]) == []
    signals = driver.feed([boundary])
    assert len(entries(signals)) == 1


# --- exits ------------------------------------------------------------------


def in_trade_driver(**kwargs: object) -> Driver:
    driver = Driver(naim(**kwargs))
    breakout = bar_1m(5, high="20030", low="20015", close="20028", open_="20015")
    driver.feed(opening() + [breakout])
    driver.feed([bar_1m(6, high="20032", low="20024", close="20030", open_="20028")])
    assert driver.strategy._state == IN_TRADE
    return driver


def test_close_back_inside_the_band_exits() -> None:
    driver = in_trade_driver()
    # VWAP sits near 20001 after the flat opening; LB is 19980. A close at
    # 19999 is through both -- unambiguous structural violation.
    re_entry = bar_1m(7, high="20025", low="19998", close="19999", open_="20025")

    signals = driver.feed([re_entry])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert "structural stop" in signals[0].reason
    assert driver.tracker.position is None
    assert driver.strategy._state == ACTIVE  # re-entry permitted


def test_vwap_exit_fires_before_the_band_in_close_mode() -> None:
    """VWAP (~20002 here) sits above LB 19980: a close between them is
    inside the band but below VWAP, and the stop is max(LB, VWAP)."""
    driver = in_trade_driver()
    between = bar_1m(7, high="20026", low="20000", close="20001", open_="20026")

    signals = driver.feed([between])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]


def test_vwap_component_can_be_disabled() -> None:
    driver = in_trade_driver(vwap_stop=False)
    between = bar_1m(7, high="20026", low="20000", close="20001", open_="20026")

    assert driver.feed([between]) == []  # inside band, above LB: still valid


def test_touch_mode_exits_on_the_wick() -> None:
    driver = in_trade_driver(stop_mode="touch")
    wick = bar_1m(7, high="20030", low="19979", close="20027", open_="20028")

    signals = driver.feed([wick])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]


def test_close_mode_survives_the_same_wick() -> None:
    driver = in_trade_driver()
    wick = bar_1m(7, high="20030", low="19979", close="20027", open_="20028")

    assert driver.feed([wick]) == []


def test_hard_time_exit_at_1555() -> None:
    driver = in_trade_driver()
    late = bar_1m(384, high="20030", low="20024", close="20028", open_="20028")  # 15:55

    signals = driver.feed([late])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert "time" in signals[0].reason
    assert driver.strategy._state == DONE


def test_fomc_flattens_and_blocks_after_1345() -> None:
    driver = in_trade_driver(fomc_dates={SESSION})
    fomc_bar = bar_1m(254, high="20030", low="20024", close="20028", open_="20028")  # 13:45

    signals = driver.feed([fomc_bar])
    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]

    retrig = bar_1m(255, high="20035", low="20025", close="20033", open_="20025")
    assert driver.feed([retrig]) == []


def test_no_fresh_entries_after_entry_end() -> None:
    driver = Driver(naim())
    late_breakout = bar_1m(331, high="20030", low="20015", close="20028", open_="20015")  # 15:01

    assert driver.feed(opening() + [late_breakout]) == []


def test_reentry_after_structural_stop_counts_toward_the_cap() -> None:
    driver = Driver(naim(max_entries_per_day=2))
    breakout = bar_1m(5, high="20030", low="20015", close="20028", open_="20015")
    confirm = bar_1m(6, high="20032", low="20024", close="20030", open_="20028")
    stop_bar = bar_1m(7, high="20025", low="19998", close="19999", open_="20025")
    retrig = bar_1m(8, high="20031", low="20016", close="20029", open_="20016")
    confirm2 = bar_1m(9, high="20033", low="20025", close="20031", open_="20029")
    stop2 = bar_1m(10, high="20026", low="19998", close="19999", open_="20026")
    retrig3 = bar_1m(11, high="20031", low="20016", close="20029", open_="20016")

    driver.feed(opening() + [breakout, confirm, stop_bar])
    assert len(entries(driver.signals)) == 1
    driver.feed([retrig, confirm2, stop2])
    assert len(entries(driver.signals)) == 2

    assert driver.feed([retrig3]) == [], "the 2-entry cap must hold"
    assert driver.strategy._state == DONE


# --- state ------------------------------------------------------------------


def test_state_survives_a_json_round_trip_mid_trade() -> None:
    driver = in_trade_driver()

    packed = json.loads(json.dumps(driver.strategy.get_state()))
    revived = NoiseAreaIntradayMomentum(noise_curves=FLAT_CURVE)
    revived.restore_state(packed)

    assert revived.get_state() == driver.strategy.get_state()


def test_session_end_banks_prev_close() -> None:
    strategy = naim()
    driver = Driver(strategy)
    driver.feed(opening(minutes=3, close="20005"))

    strategy.on_session_end(SESSION)

    assert strategy._prev_close == Decimal("20005")


def test_registered_and_buildable() -> None:
    from nq_agent.main import STRATEGIES, build_strategy

    assert STRATEGIES["naim"] is NoiseAreaIntradayMomentum
    assert isinstance(build_strategy("naim"), NoiseAreaIntradayMomentum)
