"""level_read / day_read pure cores (spec §21: deterministic, no LLM math)."""

from pathlib import Path

from desk_brain.factors import load_factors
from desk_brain.tools.reads import compute_day_read, compute_level_read

FACTORS = load_factors(Path(__file__).resolve().parents[2] / "factors.yaml")


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
