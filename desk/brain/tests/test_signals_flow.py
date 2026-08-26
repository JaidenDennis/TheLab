"""A1 flow + A2 footprint pure signals (addendum Part A) on fixtures."""

from desk_brain.tools.signals import flow, footprint, linreg_slope, median, pct_rank


def bar(o=100.0, h=101.0, lo=99.0, c=100.5, vol=1000, delta=200, buy=None, sell=None):
    buy = buy if buy is not None else (vol + delta) / 2
    sell = sell if sell is not None else (vol - delta) / 2
    return {"o": o, "h": h, "l": lo, "c": c, "vol": vol, "buy": buy, "sell": sell, "delta": delta}


# -- helpers ----------------------------------------------------------------


def test_helpers():
    assert linreg_slope([1, 2, 3, 4]) == 1.0
    assert linreg_slope([2]) is None
    assert pct_rank([1, 2, 3, 4], 4) == 100.0
    assert median([1, 2, 3]) == 2
    assert median([]) is None


# -- A1 ---------------------------------------------------------------------


def test_cvd_series_and_slope():
    bars = [bar(delta=100), bar(delta=-50), bar(delta=200)]
    assert flow.cvd_series(bars) == [100, 50, 250]
    assert flow.cvd_slope(bars, 3) > 0


def test_delta_vol_ratio_bounds():
    assert flow.delta_vol_ratio(bar(vol=1000, delta=500)) == 0.5
    assert flow.delta_vol_ratio({"vol": 0, "delta": 0}) is None


def test_delta_persistence_runs():
    bars = [bar(delta=10), bar(delta=20), bar(delta=30), bar(delta=-5)]
    out = flow.delta_persistence(bars)
    assert out["longest_run"] == 3 and out["longest_sign"] == "buy"
    assert out["current_run"] == 1 and out["current_sign"] == "sell"


def test_cvd_divergence_bearish():
    # price pushes to a new high while the deltas dry up
    bars = [bar(h=100 + i, delta=500 - i * 100) for i in range(5)]
    bars.append(bar(h=110, delta=-400))  # new high, negative delta -> CVD below prior max
    out = flow.cvd_divergence(bars, lookback=6)
    assert out and out["kind"] == "bearish"


def test_cvd_divergence_none_when_flow_confirms():
    bars = [bar(h=100 + i, delta=300) for i in range(6)]
    assert flow.cvd_divergence(bars, lookback=6) is None


def test_delta_at_extremes():
    bars = [bar(h=105, lo=99, delta=50), bar(h=110, lo=100, delta=-80), bar(h=104, lo=95, delta=30)]
    out = flow.delta_at_extremes(bars)
    assert out["high"]["price"] == 110 and out["high"]["delta"] == -80
    assert out["high"]["next_delta"] == 30
    assert out["low"]["price"] == 95 and out["low"]["next_delta"] is None


def test_delta_rate_windows():
    now = 1000.0
    ticks = [(now - 5, 100.0, 10, 1), (now - 20, 100.0, 20, -1), (now - 50, 100.0, 30, 1)]
    out = flow.delta_rate(ticks, now, [10, 30, 60])
    assert out["10s"] == 1.0  # +10 over 10s
    assert out["30s"] == round((10 - 20) / 30, 2)
    assert out["60s"] == round((10 - 20 + 30) / 60, 2)


# -- A2 ---------------------------------------------------------------------


def cells_buy_stack():
    # aggressive buying overwhelming the bid diagonal at 4 consecutive prices
    return {
        100.0: {"buy": 90.0, "sell": 10.0, "max_print": 30.0},
        100.25: {"buy": 95.0, "sell": 10.0, "max_print": 20.0},
        100.5: {"buy": 80.0, "sell": 8.0, "max_print": 15.0},
        100.75: {"buy": 60.0, "sell": 5.0, "max_print": 10.0},
        101.0: {"buy": 0.0, "sell": 3.0, "max_print": 3.0},
    }


def test_merge_cells_sums():
    merged = footprint.merge_cells([{100.0: {"buy": 5, "sell": 1, "max_print": 5}},
                                    {100.0: {"buy": 3, "sell": 2, "max_print": 8}}])
    assert merged[100.0] == {"buy": 8, "sell": 3, "max_print": 8}


def test_bar_poc_and_position():
    cells = cells_buy_stack()
    poc = footprint.bar_poc(cells)
    assert poc == 100.25
    assert footprint.poc_position(poc, high=101.0, low=100.0) == "low"
    assert footprint.poc_migration([100.0, 100.25]) == "rising"
    assert footprint.poc_migration([None]) is None


def test_diagonal_and_stacked_imbalances():
    imb = footprint.diagonal_imbalances(cells_buy_stack(), ratio=3.0, tick=0.25)
    sides = {i["side"] for i in imb}
    assert "buy" in sides
    stacks = footprint.stacked_imbalances(imb, min_run=3, tick=0.25)
    assert stacks and stacks[0]["side"] == "buy" and stacks[0]["n"] >= 3


def test_auction_state():
    cells = cells_buy_stack()  # high (101.0) printed zero buys -> finished
    st = footprint.auction_state(cells)
    assert st["high"] == "finished"
    assert st["low"] == "unfinished"  # low traded both sides


def test_hvn_lvn():
    out = footprint.hvn_lvn(cells_buy_stack(), hvn_mult=1.3, lvn_mult=0.3)
    assert 100.25 in out["hvn"]
    assert 101.0 in out["lvn"]


def test_delta_concentration():
    assert footprint.delta_concentration(cells_buy_stack()) == "bottom"
    assert footprint.delta_concentration({}) is None
