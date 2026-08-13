"""TFR driven through the engine-faithful harness.

Decision files are synthesized per test: the strategy runtime is pure
logic over them, which is exactly what makes it testable without ticks.
July 2026 is EDT: 09:30 ET == 13:30 UTC. 5m bars close at minute indices
5, 10, ... (09:35, 09:40, ...).
"""

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.position import PositionTracker
from nq_agent.strategy.tfr import ACTIVE, DONE, IN_TRADE, TickFlowRegime

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
SESSION = date(2026, 7, 15)
CAL = SessionCalendar("America/New_York", time(9, 30), time(16, 30))
Q_TABLE = {"55": 0.10, "60": 0.12, "65": 0.14, "70": 0.15, "75": 0.18, "80": 0.22, "85": 0.28}


def bar_record(
    f1: float = 0.0,
    regime: str = "CHOP",
    t_af: float | None = 0.5,
    z_vol: float = 1.0,
    close: float = 20000.0,
    mahal: float = 1.0,
    **extra: Any,
) -> dict[str, Any]:
    record = {
        "close": close,
        "f1_2": f1,
        "f1_5": f1,
        "f1_15": f1,
        "div": 0.0,
        "z_eff": 0.0,
        "large_share": 0.3,
        "large_imb": f1,
        "z_vol": z_vol,
        "z_rv": 0.0,
        "regime": regime,
        "t_af": t_af,
        "mahal": mahal,
    }
    record.update(extra)
    return record


def day_file(bars: dict[int, dict[str, Any]], mahal_cut: float = 6.0) -> dict[str, Any]:
    return {
        SESSION.isoformat(): {
            "model": "3-monthly-test",
            "mahal_cut": mahal_cut,
            "q_f1": Q_TABLE,
            "size_cut": 5,
            "bars": {str(k): v for k, v in bars.items()},
        }
    }


def bar_5m(index: int, close: str = "20000") -> Bar:
    return Bar(
        symbol="NQ",
        timeframe="5m",
        open_time=OPEN + timedelta(minutes=index - 5),
        open=Decimal(close),
        high=Decimal(close) + 2,
        low=Decimal(close) - 2,
        close=Decimal(close),
        volume=500,
    )


class Driver:
    def __init__(self, strategy: TickFlowRegime) -> None:
        self.strategy = strategy
        self.tracker = PositionTracker()
        self.clock = SimClock(OPEN)
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


AF = dict(regime="AF", t_af=0.7, z_vol=1.0)


def entries(signals: list[Signal]) -> list[Signal]:
    return [s for s in signals if s.intent is SignalIntent.ENTRY]


# --- entries ----------------------------------------------------------------


def test_no_decisions_means_no_trades() -> None:
    driver = Driver(TickFlowRegime(decisions={}))

    assert driver.feed([bar_5m(5), bar_5m(10)]) == []
    assert driver.strategy._state == DONE


def test_armed_af_with_strong_flow_enters_long() -> None:
    decisions = day_file({5: bar_record(), 10: bar_record(f1=0.2, **AF)})
    driver = Driver(TickFlowRegime(decisions=decisions))

    signals = driver.feed([bar_5m(5), bar_5m(10)])

    entry = entries(signals)[0]
    assert entry.direction is Direction.LONG
    assert entry.entry_price == Decimal("20000.25")
    # Catastrophic stop: 0.35% of price, tick-quantized.
    assert entry.stop_price == Decimal("19930.25")
    assert entry.target_price == Decimal("21000.25")


def test_negative_flow_enters_short() -> None:
    decisions = day_file({10: bar_record(f1=-0.2, **AF)})
    driver = Driver(TickFlowRegime(decisions=decisions))

    signals = driver.feed([bar_5m(10)])

    assert entries(signals)[0].direction is Direction.SHORT


def test_each_arming_condition_blocks_alone() -> None:
    cases = [
        bar_record(f1=0.2, regime="CHOP", t_af=0.7, z_vol=1.0),  # not AF
        bar_record(f1=0.2, regime="AF", t_af=0.5, z_vol=1.0),  # t_af < p_arm
        bar_record(f1=0.2, regime="AF", t_af=0.7, z_vol=0.2),  # vol_z low
        bar_record(f1=0.10, regime="AF", t_af=0.7, z_vol=1.0),  # |F1| < Q70
    ]
    for record in cases:
        driver = Driver(TickFlowRegime(decisions=day_file({10: record})))
        assert driver.feed([bar_5m(10)]) == [], record


def test_f4_confirmation_vetoes_disagreeing_large_flow() -> None:
    record = bar_record(f1=0.2, **AF)
    record["large_imb"] = -0.1  # large trades lean the other way
    driver = Driver(TickFlowRegime(decisions=day_file({10: record}), f4_confirm=True))

    assert driver.feed([bar_5m(10)]) == []


def test_model_health_halt_stands_the_session_down() -> None:
    decisions = day_file({5: bar_record(mahal=9.0), 10: bar_record(f1=0.2, **AF)})
    driver = Driver(TickFlowRegime(decisions=decisions))

    assert driver.feed([bar_5m(5), bar_5m(10)]) == []
    assert driver.strategy._state == DONE


def test_fomc_blocks_entries_after_1300() -> None:
    late_index = 215  # closes 13:05 ET
    decisions = day_file({late_index: bar_record(f1=0.2, **AF)})
    driver = Driver(TickFlowRegime(decisions=decisions, fomc_dates={SESSION}))

    assert driver.feed([bar_5m(late_index)]) == []


def test_entry_cap_ends_the_day() -> None:
    hostile = bar_record(f1=-0.2, **AF)  # exits a long via V-HF, also re-arms short
    decisions = day_file(
        {
            10: bar_record(f1=0.2, **AF),
            15: bar_record(f1=0.2, **AF),
            20: hostile,
            25: bar_record(f1=0.2, **AF),
            30: bar_record(f1=0.2, **AF),
            35: hostile,
            40: bar_record(f1=0.2, **AF),
            45: bar_record(f1=0.2, **AF),
        }
    )
    driver = Driver(TickFlowRegime(decisions=decisions, exit_mode="hf", max_entries_per_day=2))

    driver.feed([bar_5m(i) for i in range(10, 50, 5)])

    assert len(entries(driver.signals)) == 2
    assert driver.strategy._state == DONE


# --- exits ------------------------------------------------------------------


def in_trade(exit_mode: str, extra_bars: dict[int, dict[str, Any]], **kwargs: Any) -> Driver:
    bars = {10: bar_record(f1=0.2, **AF), 15: bar_record(f1=0.2, **AF)}
    bars.update(extra_bars)
    driver = Driver(TickFlowRegime(decisions=day_file(bars), exit_mode=exit_mode, **kwargs))
    driver.feed([bar_5m(10), bar_5m(15)])
    assert driver.strategy._state == IN_TRADE
    return driver


def test_hostile_flow_exits_hf() -> None:
    driver = in_trade("hf", {20: bar_record(f1=-0.2, regime="AF", t_af=0.7)})

    signals = driver.feed([bar_5m(20)])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert "hostile flow" in signals[0].reason


def test_neutral_flow_does_not_exit_hf() -> None:
    driver = in_trade("hf", {20: bar_record(f1=0.0, regime="CHOP", t_af=0.2)})

    assert driver.feed([bar_5m(20)]) == []  # HF holds through quiet death


def test_regime_invalidation_needs_two_consecutive_bars() -> None:
    driver = in_trade(
        "ri",
        {
            20: bar_record(f1=0.05, regime="CHOP", t_af=0.7),
            25: bar_record(f1=0.05, regime="AF", t_af=0.7),  # flicker resets
            30: bar_record(f1=0.05, regime="CHOP", t_af=0.7),
            35: bar_record(f1=0.05, regime="CHOP", t_af=0.7),
        },
    )

    assert driver.feed([bar_5m(20), bar_5m(25), bar_5m(30)]) == []
    signals = driver.feed([bar_5m(35)])
    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert "regime invalidation" in signals[0].reason


def test_retention_prob_collapse_also_invalidates() -> None:
    driver = in_trade(
        "ri",
        {
            20: bar_record(f1=0.05, regime="AF", t_af=0.3),
            25: bar_record(f1=0.05, regime="AF", t_af=0.3),
        },
    )

    signals = driver.feed([bar_5m(20), bar_5m(25)])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]


def test_stack_takes_whichever_fires_first() -> None:
    # Hostile flow on the first bar after entry: HF beats RI's 2-bar wait.
    driver = in_trade("stack", {20: bar_record(f1=-0.2, regime="CHOP", t_af=0.2)})

    signals = driver.feed([bar_5m(20)])

    assert "hostile flow" in signals[0].reason


def test_t13_baseline_exits_on_time() -> None:
    quiet = {i: bar_record(f1=0.05, regime="AF", t_af=0.7) for i in range(20, 100, 5)}
    driver = in_trade("t13", quiet, t13_bars=3)

    signals = driver.feed([bar_5m(20), bar_5m(25), bar_5m(30)])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert "time baseline" in signals[0].reason


def test_hard_flatten_at_1555() -> None:
    index = 385  # closes 15:55 ET
    driver = in_trade("hf", {index: bar_record(f1=0.2, **AF)})

    signals = driver.feed([bar_5m(index)])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]
    assert "15:55" in signals[0].reason
    assert driver.strategy._state == DONE


def test_stop_and_reverse_flattens_then_enters_opposite() -> None:
    decisions = day_file(
        {
            10: bar_record(f1=0.2, **AF),
            15: bar_record(f1=0.2, **AF),
            20: bar_record(f1=-0.2, **AF),  # fully qualified opposite signal
            25: bar_record(f1=-0.2, **AF),
        }
    )
    driver = Driver(TickFlowRegime(decisions=decisions, exit_mode="ri"))
    driver.feed([bar_5m(10), bar_5m(15)])

    flat = driver.feed([bar_5m(20)])
    assert "reverse" in flat[0].reason
    entered = driver.feed([bar_5m(25)])
    assert entries(entered)[0].direction is Direction.SHORT


# --- state ------------------------------------------------------------------


def test_state_survives_a_json_round_trip_mid_trade() -> None:
    driver = in_trade("stack", {})

    packed = json.loads(json.dumps(driver.strategy.get_state()))
    revived = TickFlowRegime(decisions=day_file({}))
    revived.restore_state(packed)

    assert revived.get_state() == driver.strategy.get_state()
    assert revived._state == IN_TRADE


def test_resumed_in_trade_keeps_managing() -> None:
    """A restored IN_TRADE must re-derive the decision file on its first bar
    and keep applying exits, not stand stranded."""
    driver = in_trade("hf", {20: bar_record(f1=-0.2, regime="AF", t_af=0.7)})
    packed = json.loads(json.dumps(driver.strategy.get_state()))

    revived = TickFlowRegime(
        decisions=day_file(
            {
                10: bar_record(f1=0.2, **AF),
                15: bar_record(f1=0.2, **AF),
                20: bar_record(f1=-0.2, regime="AF", t_af=0.7),
            }
        ),
        exit_mode="hf",
    )
    revived.restore_state(packed)
    driver2 = Driver(revived)
    driver2.tracker.restore(driver.tracker.position)

    signals = driver2.feed([bar_5m(20)])

    assert [s.intent for s in signals] == [SignalIntent.FLATTEN]


def test_registered_and_buildable() -> None:
    from nq_agent.main import STRATEGIES, build_strategy

    assert STRATEGIES["tfr"] is TickFlowRegime
    assert isinstance(build_strategy("tfr"), TickFlowRegime)
