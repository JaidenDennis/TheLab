"""AFR through the engine-faithful harness. Decision records synthesized;
same Driver pattern as TFR's tests."""

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.position import PositionTracker
from nq_agent.strategy.afr import DONE, IN_TRADE, AbsorptionFlowReversal

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
SESSION = date(2026, 7, 15)
CAL = SessionCalendar("America/New_York", time(9, 30), time(16, 30))
Q_TABLE = {"70": 0.15, "80": 0.22}


def bar_record(f1: float = 0.0, z_eff: float = 0.0, close: float = 20000.0) -> dict[str, Any]:
    return {"close": close, "f1_5": f1, "z_eff": z_eff, "z_vol": 1.0,
            "regime": None, "t_af": None, "mahal": None}


def day_file(bars: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {SESSION.isoformat(): {"model": "flow", "mahal_cut": None, "q_f1": Q_TABLE,
                                  "size_cut": 5, "bars": {str(k): v for k, v in bars.items()}}}


def bar_5m(index: int, close: str = "20000") -> Bar:
    return Bar(symbol="NQ", timeframe="5m", open_time=OPEN + timedelta(minutes=index - 5),
               open=Decimal(close), high=Decimal(close) + 2, low=Decimal(close) - 2,
               close=Decimal(close), volume=500)


class Driver:
    def __init__(self, strategy: AbsorptionFlowReversal) -> None:
        self.strategy = strategy
        self.tracker = PositionTracker()
        self.clock = SimClock(OPEN - timedelta(hours=1))
        self.ctx = Context(self.clock, CAL, 500)
        self.trades = 0
        self.signals: list[Signal] = []

    def feed(self, bars: list[Bar]) -> list[Signal]:
        out = []
        for bar in bars:
            self.clock.advance_to(bar.close_time)
            self.tracker.on_bar(bar)
            self.ctx.record_bar(bar)
            self.ctx.set_position(self.tracker.position)
            self.ctx.set_trades_taken(self.trades)
            signal = self.strategy.on_bar(bar, self.ctx)
            if signal is None:
                continue
            out.append(signal)
            self.signals.append(signal)
            if signal.intent is SignalIntent.ENTRY:
                if self.tracker.on_signal(signal) is not None:
                    self.trades += 1
            else:
                self.tracker.flatten(bar.close, bar.close_time)
        return out


ABSORBED_BUY = dict(f1=0.2, z_eff=-1.4)  # heavy buying, price refusing to move


def entries(signals):
    return [s for s in signals if s.intent is SignalIntent.ENTRY]


def test_absorbed_buying_is_faded_short() -> None:
    driver = Driver(AbsorptionFlowReversal(decisions=day_file({10: bar_record(**ABSORBED_BUY)})))

    signals = driver.feed([bar_5m(10)])

    entry = entries(signals)[0]
    assert entry.direction is Direction.SHORT  # against the buy aggressor
    assert entry.entry_price == Decimal("19999.75")
    assert entry.stop_price % Decimal("0.25") == 0  # tick-grid catastrophic stop


def test_absorbed_selling_is_faded_long() -> None:
    decisions = day_file({10: bar_record(f1=-0.2, z_eff=-1.4)})
    driver = Driver(AbsorptionFlowReversal(decisions=decisions))

    assert entries(driver.feed([bar_5m(10)]))[0].direction is Direction.LONG


def test_normal_displacement_blocks_the_fade() -> None:
    # Heavy flow moving price normally is fc_t13's world, not AFR's.
    decisions = day_file({10: bar_record(f1=0.2, z_eff=0.3)})
    driver = Driver(AbsorptionFlowReversal(decisions=decisions))

    assert driver.feed([bar_5m(10)]) == []


def test_weak_flow_blocks_even_with_low_eff() -> None:
    decisions = day_file({10: bar_record(f1=0.05, z_eff=-2.0)})
    driver = Driver(AbsorptionFlowReversal(decisions=decisions))

    assert driver.feed([bar_5m(10)]) == []


def test_naive_control_enters_without_absorption() -> None:
    decisions = day_file({10: bar_record(f1=0.2, z_eff=0.3)})
    driver = Driver(AbsorptionFlowReversal(decisions=decisions, absorption_required=False))

    assert len(entries(driver.feed([bar_5m(10)]))) == 1


def in_trade(exit_mode: str, extra: dict[int, dict[str, Any]]) -> Driver:
    bars = {10: bar_record(**ABSORBED_BUY)}
    bars.update(extra)
    driver = Driver(AbsorptionFlowReversal(decisions=day_file(bars), exit_mode=exit_mode))
    driver.feed([bar_5m(10), bar_5m(15)])
    assert driver.strategy._state == IN_TRADE
    return driver


def test_continuation_stop_fires_when_aggressors_reassert() -> None:
    driver = in_trade("t13", {20: bar_record(f1=0.18, z_eff=0.5)})  # buying resumes past Q70

    signals = driver.feed([bar_5m(20)])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert "continuation" in signals[0].reason


def test_opposite_side_flow_does_not_trip_continuation() -> None:
    driver = in_trade("t13", {20: bar_record(f1=-0.18, z_eff=0.5)})  # sellers, not the faded side

    assert driver.feed([bar_5m(20)]) == []


def test_time_ladder_exits_at_three_bars() -> None:
    quiet = {i: bar_record(f1=0.02, z_eff=-0.2) for i in (20, 25, 30)}
    driver = in_trade("t3", quiet)

    signals = driver.feed([bar_5m(20), bar_5m(25)])
    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert "time t3" in signals[0].reason


def test_norm_exit_when_absorption_resolves() -> None:
    driver = in_trade("norm", {20: bar_record(f1=0.02, z_eff=0.1)})

    signals = driver.feed([bar_5m(20)])

    assert "flow normalized" in signals[0].reason


def test_hard_flatten_at_1555() -> None:
    driver = in_trade("t13", {385: bar_record(f1=0.02, z_eff=-0.5)})

    signals = driver.feed([bar_5m(385)])

    assert "15:55" in signals[0].reason and driver.strategy._state == DONE


def test_entry_cap_ends_the_day() -> None:
    resume = bar_record(f1=0.18, z_eff=0.5)  # continuation exit trigger
    bars = {}
    for i, idx in enumerate(range(10, 70, 10)):
        bars[idx] = bar_record(**ABSORBED_BUY) if i % 2 == 0 else dict(resume)
    driver = Driver(AbsorptionFlowReversal(decisions=day_file(bars), exit_mode="t13",
                                           max_entries_per_day=2))
    driver.feed([bar_5m(i) for i in range(10, 70, 5)])

    assert len(entries(driver.signals)) == 2
    assert driver.strategy._state == DONE


def test_state_round_trip() -> None:
    driver = in_trade("t6", {})
    packed = json.loads(json.dumps(driver.strategy.get_state()))
    revived = AbsorptionFlowReversal(decisions=day_file({}))
    revived.restore_state(packed)
    assert revived.get_state() == driver.strategy.get_state()
    assert revived._state == IN_TRADE


def test_registered() -> None:
    from nq_agent.main import STRATEGIES

    assert STRATEGIES["afr"] is AbsorptionFlowReversal
