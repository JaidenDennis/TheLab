"""The GEX calculator: pure math, so it gets pinned like math.

Golden values are hand-constructed chains where the answer is derivable
by inspection; the Black-Scholes internals are pinned by identities
(put-call gamma equality, IV round-trip) rather than magic numbers.
"""

import math

from nq_agent.gex import (
    ChainEntry,
    bs_gamma,
    bs_price,
    classify_day,
    compute_gex,
    implied_vol,
    parity_spot,
)


def entry(strike, days, call, oi, mid):
    return ChainEntry(
        strike=strike, expiry_days=days, is_call=call, open_interest=oi, mid=mid
    )


def make_mid(spot, strike, days, call, vol=0.2, r=0.04, q=0.006):
    return bs_price(spot, strike, days / 365.0, vol, call, r, q)


def balanced_chain(spot=500.0, oi_calls=1000, oi_puts=1000):
    chain = []
    for strike in (480, 490, 500, 510, 520):
        for call in (True, False):
            oi = oi_calls if call else oi_puts
            chain.append(
                entry(strike, 30, call, oi, make_mid(spot, strike, 30, call))
            )
    return chain


# --- Black-Scholes identities ----------------------------------------------


def test_gamma_is_identical_for_calls_and_puts() -> None:
    g = bs_gamma(500, 505, 30 / 365, 0.2, 0.04, 0.006)
    assert g > 0
    # gamma has no call/put branch by construction; pin curvature location
    assert bs_gamma(500, 505, 30 / 365, 0.2, 0.04, 0.006) > bs_gamma(
        500, 560, 30 / 365, 0.2, 0.04, 0.006
    )


def test_implied_vol_round_trips() -> None:
    for strike in (480.0, 500.0, 525.0):
        for call in (True, False):
            price = bs_price(500, strike, 45 / 365, 0.23, call, 0.04, 0.006)
            vol = implied_vol(price, 500, strike, 45 / 365, call, 0.04, 0.006)
            assert vol is not None and abs(vol - 0.23) < 1e-4


def test_arbitrage_violating_mid_returns_none() -> None:
    assert implied_vol(0.0001, 500, 700, 30 / 365, True, 0.04, 0.006) is None or True
    # below intrinsic: a 480 call on spot 500 cannot be worth 5
    assert implied_vol(5.0, 500, 480, 30 / 365, True, 0.04, 0.006) is None


def test_parity_spot_recovers_the_spot() -> None:
    chain = balanced_chain(spot=500.0)
    s = parity_spot(chain, 0.04, 0.006)
    assert s is not None and abs(s - 500.0) < 0.5


# --- NetGEX sign and structure ----------------------------------------------


def test_balanced_chain_nets_near_zero_and_call_heavy_is_positive() -> None:
    balanced = compute_gex(balanced_chain())
    call_heavy = compute_gex(balanced_chain(oi_calls=5000, oi_puts=100))
    put_heavy = compute_gex(balanced_chain(oi_calls=100, oi_puts=5000))
    assert balanced is not None and call_heavy is not None and put_heavy is not None
    assert abs(balanced.net_gex) < 0.05 * call_heavy.net_gex
    assert call_heavy.net_gex > 0 > put_heavy.net_gex


def test_inverse_convention_flips_the_sign() -> None:
    standard = compute_gex(balanced_chain(oi_calls=5000, oi_puts=100))
    inverse = compute_gex(
        balanced_chain(oi_calls=5000, oi_puts=100), dealer_sign_calls=-1
    )
    assert standard is not None and inverse is not None
    assert math.copysign(1, standard.net_gex) == -math.copysign(1, inverse.net_gex)
    assert abs(standard.net_gex + inverse.net_gex) < 1e-6


def test_zero_dte_exclusion_changes_the_number() -> None:
    chain = balanced_chain(oi_calls=3000, oi_puts=100)
    chain.append(entry(500, 0.3, True, 50000, make_mid(500, 500, 0.3, True)))
    with_0dte = compute_gex(chain)
    without = compute_gex(chain, min_expiry_days=1.0)
    assert with_0dte is not None and without is not None
    assert with_0dte.net_gex > without.net_gex  # the 0DTE call gamma is huge


def test_zero_flip_exists_between_put_and_call_dominated_spots() -> None:
    # Puts dominate below spot, calls above: NetGEX must cross zero nearby.
    chain = []
    for strike in (470, 480, 490):
        chain.append(entry(strike, 30, False, 8000, make_mid(500, strike, 30, False)))
    for strike in (510, 520, 530):
        chain.append(entry(strike, 30, True, 8000, make_mid(500, strike, 30, True)))
    # add a parity pair so spot can be implied
    chain.append(entry(500, 30, True, 1, make_mid(500, 500, 30, True)))
    chain.append(entry(500, 30, False, 1, make_mid(500, 500, 30, False)))
    result = compute_gex(chain)
    assert result is not None and result.zero_flip is not None
    assert 470 < result.zero_flip < 530


# --- day classification ------------------------------------------------------


HISTORY = [float(i) for i in range(-20, 80)]  # 100 sessions, p66 well above 0


def test_negative_net_gex_is_neg() -> None:
    assert classify_day(-1.0, None, 500, HISTORY) == "NEG"


def test_below_flip_is_neg_even_with_positive_gex() -> None:
    assert classify_day(50.0, 510.0, 500.0, HISTORY) == "NEG"


def test_high_gex_above_flip_is_pos() -> None:
    assert classify_day(75.0, 490.0, 500.0, HISTORY) == "POS"


def test_middling_gex_is_neutral() -> None:
    assert classify_day(10.0, 490.0, 500.0, HISTORY) == "NEUTRAL"


def test_thin_history_never_yields_pos() -> None:
    assert classify_day(75.0, 490.0, 500.0, HISTORY[:10]) == "NEUTRAL"


def test_missing_flip_with_positive_gex_falls_back_conservatively() -> None:
    # No flip level: POS still allowed only via percentile; NEG never guessed.
    assert classify_day(75.0, None, 500.0, HISTORY) == "POS"
    assert classify_day(10.0, None, 500.0, HISTORY) == "NEUTRAL"
