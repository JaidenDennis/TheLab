"""Session-Momentum Expansion, driven the way the engine drives it.

The Driver below reproduces the engine's per-bar ordering exactly -- tracker
first, then context updates, then the strategy -- because half of SME's state
machine (pending-entry confirmation, stop-out detection, re-arm) exists to
stay in agreement with that ordering. Testing it against a simplified loop
would validate a strategy for an engine that does not exist.

July 2026 is EDT, so 09:30 ET == 13:30 UTC throughout.
"""

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.position import PositionTracker
from nq_agent.strategy.sme import ARMED, DONE, IN_TRADE, SessionMomentumExpansion

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 ET
SESSION = date(2026, 7, 15)
CAL = SessionCalendar("America/New_York", time(9, 30), time(16, 30))


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


def bar_5m(minute: int, high: str, low: str, close: str, open_: str | None = None) -> Bar:
    return Bar(
        symbol="NQ",
        timeframe="5m",
        open_time=OPEN + timedelta(minutes=minute),
        open=Decimal(open_ if open_ is not None else low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=500,
    )


class Driver:
    """The engine's bar loop in miniature: real tracker, real context, same
    ordering. Risk always approves, which is exactly what the pending-entry
    logic must tolerate the absence of separately (see the veto test)."""

    def __init__(self, strategy: SessionMomentumExpansion, veto_entries: bool = False) -> None:
        self.strategy = strategy
        self.tracker = PositionTracker()
        self.clock = SimClock(OPEN - timedelta(hours=2))  # room for overnight bars
        self.ctx = Context(self.clock, CAL, 500)
        self.trades = 0
        self.signals: list[Signal] = []
        self.closes: list[object] = []
        self.veto_entries = veto_entries

    def feed(self, bars: list[Bar]) -> list[Signal]:
        emitted = []
        for bar in bars:
            self.clock.advance_to(bar.close_time)
            closed = self.tracker.on_bar(bar)
            if closed is not None:
                self.closes.append(closed)
            self.ctx.record_bar(bar)
            self.ctx.set_position(self.tracker.position)
            self.ctx.set_trades_taken(self.trades)
            signal = self.strategy.on_bar(bar, self.ctx)
            if signal is None:
                continue
            emitted.append(signal)
            self.signals.append(signal)
            if signal.intent is SignalIntent.ENTRY and not self.veto_entries:
                if self.tracker.on_signal(signal) is not None:
                    self.trades += 1
            elif signal.intent is SignalIntent.FLATTEN:
                closed = self.tracker.flatten(bar.close, bar.close_time)
                if closed is not None:
                    self.closes.append(closed)
        return emitted


def seeded(q_ofi: float | None = None, **kwargs: object) -> SessionMomentumExpansion:
    """A strategy with 20 completed sessions of plausible history restored,
    exactly as a resumed run would have it."""
    strategy = SessionMomentumExpansion(**kwargs)  # type: ignore[arg-type]
    iso = SESSION.isocalendar()
    strategy.restore_state(
        {
            # Alternating signs so sigma is small but nonzero: threshold at
            # K_bias=0.35 lands near 0.00018, far below the test opens.
            "or_rets": [0.0005 * (1 if i % 2 else -1) for i in range(20)],
            "or_ranges": [50.0] * 20,
            "daily_ranges": [100.0] * 20,
            "atr": "20",
            "q_ofi": q_ofi,
            "q_week": f"{iso.year}-{iso.week}",
        }
    )
    return strategy


def or_bars(close: str = "20098", open_: str = "20060") -> list[Bar]:
    """Thirty opening-range minutes: range [20050, 20100], OR_ret set by
    `close` against `open_`. With the seeded history: OR_mid 20075, and at
    ATR 20 the long trigger is 20102, short trigger 20048."""
    bars = [bar_1m(0, high="20100", low="20050", close="20075", open_=open_)]
    bars += [bar_1m(m, high="20090", low="20060", close="20075") for m in range(1, 29)]
    bars.append(bar_1m(29, high="20099", low="20070", close=close))
    return bars


def strong_up_bar(minute: int, high: str = "20103", close: str = "20103") -> Bar:
    """Touches the 20102 long trigger and closes on its high, so the OFI
    proxy reads it as pure buying."""
    return bar_1m(minute, high=high, low="20098", close=close, open_="20098")


def entries(signals: list[Signal]) -> list[Signal]:
    return [s for s in signals if s.intent is SignalIntent.ENTRY]


# --- layer A: context ------------------------------------------------------


def test_no_history_means_no_trades() -> None:
    """Warmup honesty: with no completed sessions to compare against, the
    context layer refuses the day outright."""
    driver = Driver(SessionMomentumExpansion(ofi_mode="off"))

    signals = driver.feed(or_bars() + [strong_up_bar(30)])

    assert signals == []
    assert driver.strategy._state == DONE


def test_an_atr_alone_is_not_enough_history() -> None:
    """Pins the session-history requirement specifically: an ATR exists
    within two days of bars, but sigma_or needs `stat_window` completed
    sessions, and without them there is nothing to measure the open
    against. (A fresh strategy also lacks the ATR, so the test above cannot
    pin this check by itself.)"""
    strategy = SessionMomentumExpansion(ofi_mode="off")
    strategy.restore_state({"atr": "20"})
    driver = Driver(strategy)

    signals = driver.feed(or_bars() + [strong_up_bar(30)])

    assert signals == []
    assert driver.strategy._state == DONE


def test_strong_open_arms_long_and_breakout_enters() -> None:
    driver = Driver(seeded(ofi_mode="off"))

    signals = driver.feed(or_bars(close="20098") + [strong_up_bar(30)])

    assert len(entries(signals)) == 1
    assert entries(signals)[0].direction is Direction.LONG


def test_neutral_open_stands_down() -> None:
    driver = Driver(seeded(ofi_mode="off"))

    signals = driver.feed(or_bars(close="20061") + [strong_up_bar(30)])

    assert signals == []
    assert driver.strategy._state == DONE


def test_weak_open_arms_short() -> None:
    driver = Driver(seeded(ofi_mode="off"))
    breakdown = bar_1m(30, high="20052", low="20047", close="20048", open_="20052")

    signals = driver.feed(or_bars(close="20020", open_="20060") + [breakdown])

    assert len(entries(signals)) == 1
    assert entries(signals)[0].direction is Direction.SHORT


def test_long_bias_ignores_a_breakdown() -> None:
    """Only the trigger matching the day's bias is armed."""
    driver = Driver(seeded(ofi_mode="off"))
    breakdown = bar_1m(30, high="20052", low="20047", close="20048", open_="20052")

    signals = driver.feed(or_bars(close="20098") + [breakdown])

    assert signals == []


def test_exhausted_overnight_stands_down() -> None:
    """An overnight that already travelled 90 points against a 100-point
    average day leaves no expansion to trade."""
    driver = Driver(seeded(ofi_mode="off"))
    overnight = [
        bar_1m(-30, high="20090", low="20000", close="20050"),  # closes 09:01 ET
        bar_1m(-2, high="20060", low="20050", close="20055"),  # closes 09:29 ET
    ]

    signals = driver.feed(overnight + or_bars() + [strong_up_bar(30)])

    assert signals == []
    assert driver.strategy._state == DONE


def test_calendar_no_trade_date_stands_down() -> None:
    driver = Driver(seeded(ofi_mode="off", no_trade_dates={SESSION}))

    signals = driver.feed(or_bars() + [strong_up_bar(30)])

    assert signals == []


def test_layer_b_alone_needs_no_session_history() -> None:
    """The ablation baseline: layer A off arms both directions off nothing
    but an ATR."""
    strategy = SessionMomentumExpansion(ofi_mode="off", layer_a=False)
    strategy.restore_state({"atr": "20"})
    driver = Driver(strategy)
    breakdown = bar_1m(30, high="20052", low="20047", close="20048", open_="20052")

    signals = driver.feed(or_bars(close="20061") + [breakdown])  # neutral open

    assert len(entries(signals)) == 1
    assert entries(signals)[0].direction is Direction.SHORT


# --- layer B: entry geometry -----------------------------------------------


def test_entry_fill_stop_and_far_target() -> None:
    driver = Driver(seeded(ofi_mode="off"))

    signals = driver.feed(or_bars() + [strong_up_bar(30)])

    entry = entries(signals)[0]
    # Stop-market at the 20102 trigger, one tick of adverse slippage.
    assert entry.entry_price == Decimal("20102.25")
    # Capped stop (entry - 1.25 * ATR20 = 25 points) is closer than OR mid.
    assert entry.stop_price == Decimal("20077.25")
    # No profit target: a far target stands in for "none".
    assert entry.target_price == Decimal("21102.25")
    assert entry.quantity == 1
    assert entry.metadata["risk_points"] == "25.00"


def test_a_gap_open_fills_at_the_open_not_the_trigger() -> None:
    driver = Driver(seeded(ofi_mode="off"))
    gap = bar_1m(30, high="20112", low="20108", close="20110", open_="20110")

    signals = driver.feed(or_bars() + [gap])

    assert entries(signals)[0].entry_price == Decimal("20110.25")


def test_r_wider_than_r_max_rejects_the_day() -> None:
    driver = Driver(seeded(ofi_mode="off", r_max_points=Decimal("10")))

    signals = driver.feed(or_bars() + [strong_up_bar(30), strong_up_bar(31)])

    assert signals == []
    assert driver.strategy._state == DONE


def test_no_entries_after_1530() -> None:
    driver = Driver(seeded(ofi_mode="off"))
    late = strong_up_bar(361)  # closes 15:32 ET

    signals = driver.feed(or_bars() + [late])

    assert signals == []


def test_a_vetoed_entry_ends_the_day() -> None:
    """If the risk layer refuses the entry, the strategy stands down rather
    than fighting the governor bar after bar."""
    driver = Driver(seeded(ofi_mode="off"), veto_entries=True)

    signals = driver.feed(
        or_bars() + [strong_up_bar(30), bar_1m(31, high="20099", low="20095", close="20097")]
    )

    assert len(entries(signals)) == 1  # emitted once
    assert driver.strategy._state == DONE  # then never again
    more = driver.feed([strong_up_bar(32)])
    assert more == []


# --- layer C: the OFI gate -------------------------------------------------


def test_an_uncalibrated_gate_fails_closed() -> None:
    driver = Driver(seeded(q_ofi=None, ofi_mode="proxy"))

    signals = driver.feed(or_bars() + [strong_up_bar(30)])

    assert signals == []
    assert driver.strategy._state == ARMED  # suppressed, not disarmed


def test_strong_flow_passes_the_gate() -> None:
    # The 5-bar proxy window at the touch spans three flat OR bars and two
    # up-closing bars, reading ~0.39; a 0.3 threshold passes it.
    driver = Driver(seeded(q_ofi=0.3, ofi_mode="proxy"))

    signals = driver.feed(or_bars() + [strong_up_bar(30)])

    assert len(entries(signals)) == 1


def test_weak_flow_suppresses_and_chase_disarms_then_rearms() -> None:
    # The same window with a mid-range close at the touch reads ~0.19,
    # under the 0.3 threshold.
    driver = Driver(seeded(q_ofi=0.3, ofi_mode="proxy"))
    # Touches the trigger but closes mid-range: proxy OFI reads ~0.
    weak = bar_1m(30, high="20103", low="20095", close="20099", open_="20098")
    # Runs beyond trigger + 0.5*ATR (20112) still closing mid-range, so the
    # gate keeps failing while price leaves without us.
    chase = bar_1m(31, high="20116", low="20110", close="20113", open_="20110")
    # Trades back inside the OR by a quarter of its range (<= 20087.50).
    dip = bar_1m(32, high="20090", low="20087", close="20089", open_="20090")

    assert driver.feed(or_bars() + [weak]) == []
    assert driver.strategy._state == ARMED

    assert driver.feed([chase]) == []
    assert driver.feed([dip]) == []
    signals = driver.feed([strong_up_bar(33)])

    assert len(entries(signals)) == 1, "the re-armed trigger with passing flow must enter"


def test_the_rearm_is_spent_by_a_second_chase_disarm() -> None:
    """Pins the one-shot specifically where the entries cap cannot shadow
    it: two chase-disarms consume zero entries, so only the re-arm flag
    stands between the second one and an endless disarm/re-arm loop."""
    driver = Driver(seeded(q_ofi=0.3, ofi_mode="proxy"))
    weak = bar_1m(30, high="20103", low="20095", close="20099", open_="20098")
    chase = bar_1m(31, high="20116", low="20110", close="20113", open_="20110")
    dip = bar_1m(32, high="20090", low="20087", close="20089", open_="20090")
    weak2 = bar_1m(33, high="20103", low="20094", close="20098", open_="20095")
    chase2 = bar_1m(34, high="20116", low="20108", close="20113", open_="20110")
    dip2 = bar_1m(35, high="20090", low="20087", close="20089", open_="20090")

    driver.feed(or_bars() + [weak, chase, dip, weak2, chase2, dip2])
    signals = driver.feed([strong_up_bar(36)])

    assert signals == [], "the single re-arm was already spent by the first disarm"
    assert driver.strategy._state == DONE


# --- management: exits and re-arms -----------------------------------------


def in_trade_driver() -> Driver:
    """Seeded, entered long at 20102.25 (stop 20077.25, R=25), confirmed."""
    driver = Driver(seeded(ofi_mode="off"))
    driver.feed(or_bars() + [strong_up_bar(30)])
    driver.feed([bar_1m(31, high="20105", low="20100", close="20104")])
    assert driver.strategy._state == IN_TRADE
    return driver


def test_break_even_then_trail_then_exit() -> None:
    driver = in_trade_driver()

    # +1R excursion (high >= 20127.25) arms break-even at entry + 2 ticks.
    driver.feed([bar_1m(32, high="20128", low="20115", close="20127")])
    assert driver.strategy._vstop == Decimal("20102.75")

    # A completed 5m bar ratchets the trail under its low.
    driver.feed([bar_5m(30, high="20128", low="20115", close="20127", open_="20100")])
    assert driver.strategy._vstop == Decimal("20114.75")

    # A 1m bar through the virtual stop exits, though the hard stop (20077.25)
    # was never touched.
    signals = driver.feed([bar_1m(35, high="20118", low="20110", close="20112")])
    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert driver.tracker.position is None
    assert driver.strategy._state == DONE  # a trailed exit ends the day


def test_hard_time_exit_at_1555() -> None:
    driver = in_trade_driver()

    signals = driver.feed([bar_1m(384, high="20120", low="20110", close="20115")])  # 15:55 ET

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert "time" in signals[0].reason
    assert driver.tracker.position is None


def test_fomc_flattens_at_1345() -> None:
    driver = Driver(seeded(ofi_mode="off", fomc_dates={SESSION}))
    driver.feed(or_bars() + [strong_up_bar(30)])
    driver.feed([bar_1m(31, high="20105", low="20100", close="20104")])

    signals = driver.feed([bar_1m(254, high="20120", low="20110", close="20115")])  # 13:45 ET

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert driver.tracker.position is None


def test_stop_out_rearms_once_then_the_day_is_done() -> None:
    driver = Driver(seeded(ofi_mode="off"))
    driver.feed(or_bars() + [strong_up_bar(30)])

    # The hard stop (20077.25) is hit: the tracker closes it before the
    # strategy sees the bar. The same bar trades back inside the OR.
    stop_bar = bar_1m(31, high="20080", low="20077", close="20079", open_="20080")
    assert driver.feed([stop_bar]) == []
    assert len(driver.closes) == 1

    # Re-break: the one re-arm produces entry number two.
    signals = driver.feed([strong_up_bar(32)])
    assert len(entries(signals)) == 1

    # Confirm, stop out again: two entries taken, re-arm spent -- done.
    driver.feed([bar_1m(33, high="20105", low="20100", close="20104")])
    assert driver.feed([bar_1m(34, high="20080", low="20077", close="20079", open_="20080")]) == []
    assert driver.feed([strong_up_bar(35)]) == []
    assert driver.strategy._state == DONE


# --- state round-trip ------------------------------------------------------


def test_state_survives_a_json_round_trip_mid_trade() -> None:
    driver = in_trade_driver()
    driver.feed([bar_1m(32, high="20128", low="20115", close="20127")])  # BE armed

    packed = json.loads(json.dumps(driver.strategy.get_state()))
    revived = SessionMomentumExpansion(ofi_mode="off")
    revived.restore_state(packed)

    assert revived.get_state() == driver.strategy.get_state()


def test_session_end_banks_rolling_stats() -> None:
    strategy = seeded(ofi_mode="off")
    driver = Driver(strategy)
    driver.feed(or_bars())

    before = list(strategy._daily_ranges)
    strategy.on_session_end(SESSION)

    assert len(strategy._daily_ranges) == len(before)  # capped at stat_window
    assert strategy._daily_ranges[-1] == 50.0  # today's 20100-20050


def test_registered_and_buildable() -> None:
    from nq_agent.main import STRATEGIES, build_strategy

    assert STRATEGIES["sme"] is SessionMomentumExpansion
    built = build_strategy("sme")
    assert isinstance(built, SessionMomentumExpansion)
    assert built.required_timeframes == ["1m", "5m"]
