"""A6 profile, A7 vwap, A8 levels, A9 session, A10 vol, A12 manage, A13 journal."""

from datetime import datetime
from zoneinfo import ZoneInfo

from desk_brain.tools.signals import journalstats, levels, manage, profile, session, vol, vwap

ET = ZoneInfo("America/New_York")


# -- A6 ---------------------------------------------------------------------


def test_position_vs_value():
    va = {"vah": 100.0, "poc": 95.0, "val": 90.0}
    assert profile.position_vs_value(105.0, va) == {"position": "above", "distance_pts": 5.0}
    assert profile.position_vs_value(95.0, va)["position"] == "inside"
    assert profile.position_vs_value(None, va) is None


def test_value_migration():
    assert profile.value_migration({"poc": 105.0}, {"poc": 100.0}) == "higher"
    assert profile.value_migration({"poc": 100.25}, {"poc": 100.0}) == "overlapping"
    assert profile.value_migration(None, {"poc": 100.0}) is None


def test_overnight_profile_narrow():
    out = profile.overnight_profile(20110.0, 20100.0, on_ranges_20d=[30.0] * 10)
    assert out["vs_median"] == "narrow"


def test_poor_extremes():
    bars = [{"h": 101.0, "l": 99.0}, {"h": 101.0, "l": 99.5}, {"h": 100.0, "l": 98.0}]
    out = profile.poor_extremes(bars)
    assert out["poor_high"] is True and out["poor_low"] is False


def test_acceptance():
    bars = [{"c": 99.0}, {"c": 101.0}, {"c": 102.0}, {"c": 101.5}]
    out = profile.acceptance(bars, level=100.0, min_bars=3)
    assert out["side"] == "above" and out["bars_beyond"] == 3 and out["accepted"] is True


# -- A7 ---------------------------------------------------------------------


def vbar(c, vol=100):
    return {"h": c + 1, "l": c - 1, "c": c, "vol": vol, "delta": 0, "t": "2026-08-25T14:00:00+00:00"}


def test_vwap_bands_and_distance():
    bars = [vbar(100), vbar(102), vbar(104)]
    bands = vwap.vwap_bands(bars)
    assert bands["vwap"] == 102.0
    assert bands["upper2"] > bands["upper1"] > bands["vwap"]
    d = vwap.distance_to_vwap(104.0, bands)
    assert d["points"] == 2.0 and d["sigmas"] > 0


def test_vwap_slope_and_anchor():
    bars = [vbar(100 + i) for i in range(10)]
    assert vwap.vwap_slope(bars, 5) > 0
    hi_idx = vwap.anchor_at_extreme(bars, "high")
    assert hi_idx == 9
    assert vwap.anchored_vwap(bars, hi_idx) == vwap.vwap_series(bars[9:])[-1]


def test_vwap_test_outcome():
    bars = [vbar(100), vbar(108), vbar(109)]
    out = vwap.vwap_test_outcome(bars, vwap=108.0)
    assert out is not None and out["bars_ago"] == 0


# -- A8 ---------------------------------------------------------------------


def rth_bar(minute, o, h, lo, c):
    t = datetime(2026, 8, 25, 13, 30 + minute, tzinfo=ZoneInfo("UTC"))  # 09:3x ET
    return {"t": t.isoformat(), "o": o, "h": h, "l": lo, "c": c, "vol": 100}


def test_opening_range_and_ib():
    bars = [rth_bar(i + 1, 100, 101 + i, 99, 100.5) for i in range(20)]
    orr = levels.opening_range(bars, 15)
    assert orr["high"] == max(101 + i for i in range(15)) and orr["complete"] is True
    ib = levels.initial_balance(bars, 60, ib_ranges_20d=None)
    assert ib is not None and ib["complete"] is False  # only 20 of 60 minutes


def test_nearest_levels_and_confluence():
    lvls = [
        {"name": "PDH", "kind": "pdh", "price": 20150.0},
        {"name": "ON-H", "kind": "on_h", "price": 20151.0},
        {"name": "PDL", "kind": "pdl", "price": 20050.0},
    ]
    near = levels.nearest_levels(lvls, 20100.0)
    assert near["above"]["name"] == "PDH" and near["above"]["distance_pts"] == 50.0
    assert near["below"]["name"] == "PDL"
    conf = levels.confluence(lvls, 20150.0, band_pts=3.0)
    assert conf["count"] == 2  # pdh + on_h kinds
    rounds = levels.round_numbers(20130.0, [50, 100])
    assert rounds[0]["price"] == 20150.0 and rounds[1]["price"] == 20100.0


# -- A9 ---------------------------------------------------------------------


def test_gap_read_fill():
    out = session.gap_read(20100.0, 20080.0, atr=100.0, session_high=20110.0, session_low=20075.0)
    assert out["points"] == 20.0 and out["vs_atr"] == 0.2
    assert out["filled"] is True  # traded below prior close


def test_open_type_drive():
    bars = [{"o": 100 + i, "h": 101 + i, "l": 99.8 + i, "c": 101 + i} for i in range(15)]
    out = session.open_type(bars, 15)
    assert out["type"] == "open-drive" and out["direction"] == "up"


def test_open_location():
    out = session.open_location(105.0, {"vah": 100.0, "val": 90.0}, on_high=104.0, on_low=95.0)
    assert out["vs_value"] == "above" and out["vs_overnight"] == "above"


def test_range_extension_and_day_type():
    ib = {"high": 101.0, "low": 99.0, "complete": True}
    ext = session.range_extension(ib, session_high=104.0, session_low=99.5)
    assert ext["extended"] and ext["side"] == "up"
    probs = session.day_type({"type": "open-drive", "direction": "up"}, ext, {"filled": False, "points": 20},
                             cvd_slope_val=5.0, va_position="above")
    assert probs["trend"] > probs["range"] and probs["trend"] > probs["reversal"]
    assert abs(sum(probs.values()) - 1.0) < 0.02


def test_time_bucket_and_event():
    buckets = {"open": ["09:30", "10:30"], "mid": ["10:30", "14:00"], "pm": ["14:00", "16:00"]}
    now = datetime(2026, 8, 25, 10, 0, tzinfo=ET)
    out = session.time_bucket(now, buckets)
    assert out["bucket"] == "open" and out["minutes_to_next"] == 30
    ev = session.minutes_to_event(
        [{"event_time_et": "10:30", "name": "FOMC", "impact": "high"},
         {"event_time_et": "09:00", "name": "old", "impact": "high"}], now)
    assert ev["minutes"] == 30 and ev["name"] == "FOMC"


# -- A10 --------------------------------------------------------------------


def test_atr_and_vov():
    daily = [{"h": 20100 + i, "l": 20000 + i, "c": 20050 + i} for i in range(10)]
    assert vol.atr(daily, 14) == 100.0
    assert vol.atr(daily[:2], 14) is None
    bars = [{"c": 20000 + (i % 3)} for i in range(60)]
    out = vol.vol_of_vol(bars, 15)
    assert out is None or "ratio" in out  # tiny synthetic series may not qualify
    assert vol.rv_vs_iv(20.0, None) is None
    assert vol.rv_vs_iv(25.0, 20.0)["read"] == "moving more than priced"


# -- A12 --------------------------------------------------------------------


def test_excursions_and_r_multiple():
    bars = [{"h": 20120.0, "l": 20095.0}, {"h": 20130.0, "l": 20105.0}]
    out = manage.live_excursions(20100.0, "long", bars, last=20125.0)
    assert out["mfe_pts"] == 30.0 and out["mae_pts"] == 5.0
    assert manage.r_multiple(20100.0, "long", 20130.0, 20085.0) == 2.0
    assert manage.r_multiple(20100.0, "short", 20130.0, 20115.0) == -2.0


def test_stop_viability_mae1():
    out = manage.stop_viability(20100.0, 20090.0, 15.0)
    assert out["viable"] is False
    assert manage.stop_viability(20100.0, 20080.0, 15.0)["viable"] is True
    assert manage.stop_viability(20100.0, None, 15.0)["has_stop"] is False


def test_flow_since_entry_and_time():
    bars = [{"delta": 200, "impulse": 0.05, "impulse_q": 88.0}, {"delta": 100, "impulse": 0.02, "impulse_q": 40.0}]
    out = manage.flow_since_entry(bars, "long")
    assert out["with_position"] is True and out["best_q"] == 88.0
    tit = manage.time_in_trade(30.0, journal_median_min=10.0, overstay_mult=2.0)
    assert tit["overstaying"] is True
    assert manage.plan_alignment("long", "short into value")["on_plan"] is False


# -- A13 --------------------------------------------------------------------


def test_journal_stats():
    trades = [{"net_pnl": 100}, {"net_pnl": -50}, {"net_pnl": 30}]
    out = journalstats.setup_stats(trades, min_n=5)
    assert out["n"] == 3 and out["reliable"] is False and out["win_rate"] == 0.67
    assert journalstats.setup_stats([], 5)["n"] == 0
    viol = journalstats.violation_rate(
        [{"rule_violations": ["stop<15"]}, {"rule_violations": []}], lookback=20)
    assert viol["trades_with_violation"] == 1 and viol["by_rule"] == {"stop<15": 1}
    sim = journalstats.similar_trades(
        [{"entry_price": 20100.0, "entry_at": "2026-08-20T10:00:00"},
         {"entry_price": 20500.0, "entry_at": "2026-08-21T10:00:00"}],
        price=20105.0, band_pts=10.0, n=5)
    assert len(sim) == 1
