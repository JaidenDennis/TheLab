"""level_read / day_read / manage_read pure cores (spec §21: deterministic, no LLM math)."""

from pathlib import Path

from desk_brain.factors import load_factors
from desk_brain.tools.reads import compute_day_read, compute_level_read, compute_manage_read
from desk_brain.tools.signals import load_params

FACTORS = load_factors(Path(__file__).resolve().parents[2] / "factors.yaml")
PARAMS = load_params(Path(__file__).resolve().parents[2] / "signals.yaml")


def bars(impulse: float, q: float) -> list[dict]:
    return [{"impulse": impulse, "impulse_q": q, "c": 20150.0}]


MARKET = {
    "last": 20150.0,
    "vwap": 20140.0,
    "prior_value_area": {"vah": 20130.0, "poc": 20100.0, "val": 20070.0},
    "on_high": 20160.0,
    "on_low": 20080.0,
    "cum_delta_rth": 5000,
}


def test_level_read_no_go_below_q90():
    out = compute_level_read(20150.0, MARKET, bars(0.04, 71.0), {"levels": []}, {"gamma_sign": "NEG"}, FACTORS)
    assert out["lean"] == "no-go"
    assert "Q71" in out["reason"]
    tags = {f["key"]: f["tag"] for f in out["factors"]}
    assert tags["flow_impulse_qrank"] == "validated"
    assert tags["value_area_position"] == "discretionary"


def test_level_read_confirms_with_flow():
    out = compute_level_read(20150.0, MARKET, bars(0.09, 94.0), {"levels": []}, {"gamma_sign": "NEG"}, FACTORS)
    assert out["lean"] == "long"


def test_level_read_gamma_penalty_downgrades():
    out = compute_level_read(20150.0, MARKET, bars(0.09, 94.0), {"levels": []}, {"gamma_sign": "POS"}, FACTORS)
    assert "reduced" in out["lean"]
    assert "positive gamma" in out["reason"]


def test_level_read_stop_viability_per_mae1():
    out = compute_level_read(20150.0, MARKET, bars(0.02, 50.0), {"levels": []}, None, FACTORS)
    stop = next(f for f in out["factors"] if f["key"] == "stop_viability")
    assert stop["value"]["min_stop_pts"] == 15
    assert stop["value"]["long_stop"] == 20135.0
    assert stop["value"]["short_stop"] == 20165.0
    assert stop["tag"] == "validated"


def test_level_read_no_view_without_flow():
    out = compute_level_read(20150.0, MARKET, [], {"levels": []}, None, FACTORS)
    assert out["lean"] == "no view"


def test_day_read_no_bias_when_factors_disagree():
    market = dict(MARKET)
    market["cum_delta_rth"] = -4000  # delta short, price above vwap/poc: disagreement
    out = compute_day_read(market, bars(0.01, 40.0), {"levels": []}, {"gamma_sign": "NEG"}, FACTORS)
    assert out["lean"] == "no bias"
    assert out["flip_condition"]


def test_day_read_lean_long_when_aligned():
    out = compute_day_read(MARKET, bars(0.05, 80.0), {"levels": []}, {"gamma_sign": "NEG"}, FACTORS)
    assert out["lean"] == "long"
    assert any(f["tag"] == "validated" for f in out["factors"])


def test_day_read_carries_day_type_probs():
    out = compute_day_read(MARKET, bars(0.05, 80.0), {"levels": []}, {"gamma_sign": "NEG"}, FACTORS, params=PARAMS)
    probs = out["day_type_probs"]
    assert set(probs) == {"trend", "range", "reversal"}
    assert abs(sum(probs.values()) - 1.0) < 0.05


def test_level_read_enriched_with_journal_and_flip():
    trades = [{"net_pnl": 120.0}, {"net_pnl": -80.0}]
    out = compute_level_read(
        20150.0, MARKET, bars(0.04, 71.0), {"levels": []}, {"gamma_sign": "NEG"}, FACTORS,
        journal_trades=trades, params=PARAMS,
    )
    assert out["flip_condition"]
    stats = next(f for f in out["factors"] if isinstance(f["value"], dict) and f["value"].get("n") == 2)
    assert stats["value"]["reliable"] is False


# -- manage_read ------------------------------------------------------------


def position_doc(side="long", entry=20100.0, stop=20080.0, target=20160.0, entered_at="2026-08-25T14:00:00+00:00"):
    orders = []
    if stop is not None:
        orders.append({"contract": "MNQU6", "type": "Stop", "price": stop})
    if target is not None:
        orders.append({"contract": "MNQU6", "type": "Limit", "price": target})
    return {
        "positions": [{"contract": "MNQU6", "side": side, "size": 4, "avg_price": entry,
                       "unrealized": 200.0, "entered_at": entered_at}],
        "working_orders": orders,
    }


def bars_1m(n=10, c=20125.0, delta=300):
    return [{"t": f"2026-08-25T14:{i:02d}:00+00:00", "o": c - 1, "h": c + 2, "l": c - 2, "c": c,
             "vol": 1000, "buy": 650, "sell": 350, "delta": delta, "impulse": 0.05, "impulse_q": 85.0}
            for i in range(1, n + 1)]


def test_manage_read_flat():
    out = compute_manage_read(None, MARKET, [], [], {"levels": []}, FACTORS, params=PARAMS)
    assert out["in_trade"] is False and out["verdict"] is None


def test_manage_read_hold_when_nothing_against():
    out = compute_manage_read(
        position_doc(), {"last": 20110.0}, bars_1m(), [], {"levels": []}, FACTORS, params=PARAMS)
    assert out["in_trade"] is True
    assert out["verdict"] == "hold"
    assert out["r_multiple"] == 0.5
    stop = next(f for f in out["factors"] if f["key"] == "stop_viability")
    assert stop["tag"] == "validated" and stop["value"]["viable"] is True


def test_manage_read_exit_on_unviable_stop():
    out = compute_manage_read(
        position_doc(stop=20095.0), {"last": 20110.0}, bars_1m(), [], {"levels": []}, FACTORS, params=PARAMS)
    assert out["verdict"] == "exit"
    assert "noise" in out["reason"]


def test_manage_read_exit_into_event():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now_et = datetime(2026, 8, 25, 9, 57, tzinfo=ZoneInfo("America/New_York"))
    out = compute_manage_read(
        position_doc(), {"last": 20110.0}, bars_1m(), [], {"levels": []}, FACTORS,
        events=[{"event_time_et": "10:00", "name": "ISM", "impact": "high"}],
        params=PARAMS, now_et=now_et)
    assert out["verdict"] == "exit" and "news" in out["reason"]


def test_manage_read_trail_when_paid():
    out = compute_manage_read(
        position_doc(), {"last": 20135.0}, bars_1m(c=20135.0), [], {"levels": []}, FACTORS, params=PARAMS)
    assert out["verdict"] == "trail"  # +35 on 20-pt risk = 1.75R, flow still long
