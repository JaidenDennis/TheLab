"""A3 behavior, A4 tape, A5 depth signals + the engine tick window."""

from datetime import datetime, timezone
from decimal import Decimal

from desk_brain.state.tickwindow import TickWindow
from desk_brain.tools.signals import behavior, depth, tape

from nq_agent.models import Tick

NOW = 10_000.0


def ticks_absorbing(center=20100.0):
    """Heavy one-sided selling pinned in a 2-tick band."""
    return [(NOW - i, center - (0.25 if i % 3 else 0.0), 20, -1) for i in range(25)]


# -- A3 ---------------------------------------------------------------------


def test_absorption_detects_pinned_aggression():
    out = behavior.absorption(ticks_absorbing(), NOW, 20100.0, band_ticks=2,
                              min_vol=200, window_s=30, max_range_ticks=2)
    assert out is not None
    assert out["aggressor_side"] == "sell" and out["absorbing_side"] == "buy"
    assert out["aggressor_vol"] >= 200


def test_absorption_none_when_price_moves():
    moving = [(NOW - i, 20100.0 - i * 0.25, 30, -1) for i in range(20)]
    assert behavior.absorption(moving, NOW, 20098.0, 2, 200, 30, 2) is None


def test_exhaustion_fires_on_fading_push():
    base = [{"o": 100, "h": 101, "l": 99, "c": 100, "vol": 1000, "buy": 600, "sell": 400} for _ in range(6)]
    push = [
        {"o": 100, "h": 102, "l": 100, "c": 102, "vol": 900, "buy": 800, "sell": 100},
        {"o": 102, "h": 104, "l": 102, "c": 104, "vol": 700, "buy": 500, "sell": 200},
        {"o": 104, "h": 106, "l": 104, "c": 106, "vol": 500, "buy": 300, "sell": 200},
        {"o": 106, "h": 107, "l": 106, "c": 106.5, "vol": 200, "buy": 40, "sell": 160},
    ]
    out = behavior.exhaustion(base + push, falling_bars=3, final_vol_mult=0.5)
    assert out is not None and out["direction"] == "up"


def test_exhaustion_none_on_steady_push():
    bars = [{"o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 101 + i, "vol": 1000, "buy": 700, "sell": 300}
            for i in range(8)]
    assert behavior.exhaustion(bars, 3, 0.5) is None


def test_initiative_vs_responsive():
    va = {"vah": 100.0, "val": 90.0}
    assert behavior.initiative_vs_responsive(105.0, 500, va) == "initiative"
    assert behavior.initiative_vs_responsive(105.0, -500, va) == "responsive"
    assert behavior.initiative_vs_responsive(95.0, 500, va) == "responsive"
    assert behavior.initiative_vs_responsive(95.0, 500, None) is None


def test_effort_vs_result_flags_high_effort():
    bars = [{"h": 101, "l": 99, "vol": 500} for _ in range(9)]
    bars.append({"h": 100.3, "l": 100.0, "vol": 3000})  # huge volume, tiny range
    out = behavior.effort_vs_result(bars)
    assert out["high_effort_small_result"] is True


def test_trapped_traders():
    stack = {"side": "buy", "low": 100.0, "high": 100.75}
    later = [{"c": 100.5}, {"c": 99.5}]
    out = behavior.trapped_traders(stack, later, confirm_bars=3)
    assert out and out["trapped"] == "buyers"
    assert behavior.trapped_traders(stack, [{"c": 101.0}], 1) is None


def test_failed_breakout_and_reversal_delta():
    bars = [
        {"o": 99.5, "h": 100.0, "l": 99.0, "c": 99.8, "delta": 100},
        {"o": 99.8, "h": 101.0, "l": 99.7, "c": 100.4, "delta": 300},  # sweeps above 100
        {"o": 100.4, "h": 100.5, "l": 99.0, "c": 99.4, "delta": -600},  # reclaims
    ]
    out = behavior.failed_breakout(bars, level=100.0, beyond_ticks=2, return_bars=2)
    assert out and out["side"] == "high" and "SFB" in out["note"]
    assert behavior.reversal_delta(bars[1], bars[2], mult=1.5) is True
    assert behavior.continuation_delta({"delta": 400}, {"delta": 250}, mult=0.5) is True
    assert behavior.continuation_delta({"delta": 400}, {"delta": -250}, mult=0.5) is False


# -- A4 ---------------------------------------------------------------------


def test_large_prints_and_clusters():
    ticks = [(NOW - 1, 20100.0, 30, 1), (NOW - 2, 20100.25, 40, 1), (NOW - 3, 20100.0, 28, 1),
             (NOW - 4, 20100.0, 5, -1)]
    out = tape.large_prints(ticks, threshold=25)
    assert len(out) == 3 and out[0]["size"] == 30  # newest first
    clusters = tape.print_clusters(ticks, 25, min_prints=3, window_s=10, band_ticks=2)
    assert clusters and clusters[0]["side"] == "buy" and clusters[0]["n"] == 3


def test_tape_speed_and_spike():
    ticks = [(NOW - i * 0.1, 20100.0, 1, 1) for i in range(50)]  # 10/s
    speed = tape.tape_speed(ticks, NOW, 5)
    assert speed == 10.0
    assert tape.is_speed_spike(speed, [1.0] * 99 + [2.0], 95) is True
    assert tape.is_speed_spike(1.0, [2.0] * 100, 95) is False


def test_aggression_at_level_and_size_shift():
    ticks = [(NOW - 5, 20100.0, 10, 1), (NOW - 6, 20100.25, 8, -1), (NOW - 7, 20090.0, 50, 1)]
    out = tape.aggression_at_level(ticks, NOW, 20100.0, window_s=60)
    assert out["lifting_vol"] == 10 and out["hitting_vol"] == 8 and out["prints"] == 2
    shift = tape.print_size_shift([(NOW - 1, 0, 30, 1)], NOW, 60, session_median=5.0)
    assert shift["shift"] == "bigger"


# -- A5 ---------------------------------------------------------------------


def book(ts, bid_sz=100, ask_sz=50):
    return {"ts": ts, "bids": [[20099.75, bid_sz], [20099.5, 80]], "asks": [[20100.0, ask_sz], [20100.25, 60]]}


def test_book_imbalance_and_resting():
    b = book(NOW)
    assert depth.book_imbalance(b, 2) == round(180 / 110, 2)
    assert depth.resting_size(b, 20100.0) == {"price": 20100.0, "side": "ask", "size": 50}
    assert depth.resting_size(b, 20105.0) is None


def test_liquidity_pull_vs_filled():
    books = [book(NOW - 1, ask_sz=500), book(NOW, ask_sz=50)]
    out = depth.liquidity_pull(books, [], 20100.0, drop_frac=0.6, window_s=2)
    assert out and out["read"] == "wall pulled, not filled"
    # same drop but prints ate it -> not a pull
    prints = [(NOW - 0.5, 20100.0, 400, 1)]
    assert depth.liquidity_pull(books, prints, 20100.0, 0.6, 2) is None


def test_iceberg_detection():
    prints = [(NOW - i, 20100.0, 60, 1) for i in range(4)]
    out = depth.iceberg(book(NOW, ask_sz=50), prints, 20100.0, fill_mult=3.0, window_s=5)
    assert out and out["traded_through"] == 240


def test_depth_vacuum():
    thin = {"ts": NOW, "bids": [[20099.75, 2]], "asks": [[20100.0, 3]]}
    out = depth.depth_vacuum(thin, 20100.0, band_ticks=5, session_totals=[100.0] * 50, pctile=10)
    assert out and out["read"] == "thin book near price"


# -- tick window ------------------------------------------------------------


def mk_tick(price, size, side, ts):
    return Tick(symbol="NQ", ts=ts, price=Decimal(str(price)), size=size, side=side)


def test_tickwindow_aggressor_median_and_prune():
    w = TickWindow()
    t0 = datetime(2026, 8, 25, 14, 0, 0, tzinfo=timezone.utc)
    w.on_tick(mk_tick("20100.00", 3, "B", t0))
    w.on_tick(mk_tick("20100.25", 7, None, t0))  # uptick -> buy
    w.on_tick(mk_tick("20100.00", 2, "A", t0))
    assert [d for _, _, _, d in w.snapshot()] == [1, 1, -1]
    assert w.session_median_print() == 3.0
    late = datetime(2026, 8, 25, 14, 30, 0, tzinfo=timezone.utc)  # > WINDOW_S later
    w.on_tick(mk_tick("20101.00", 1, "B", late))
    assert len(w.snapshot()) == 1  # old prints pruned, session histogram kept
    assert sum(w.size_counts.values()) == 4
