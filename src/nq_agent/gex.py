"""Net dealer gamma (GEX) from an EOD options-chain snapshot -- pure math.

Chain snapshot in -> (NetGEX, ZeroFlip, day regime) out. No I/O, no
network: the snapshot job archives raw records and calls this, so every
regime decision is reproducible from the archive forever.

Conventions, stated so they can be criticized (GFM spec section 2.2):

- Dealer inventory sign: +1 calls, -1 puts (dealers long calls, short
  puts). AN ASSUMPTION -- the robustness sweep recomputes everything
  under the inverse convention, and if conclusions flip, the measurement
  is too fragile to trade.
- IV is solved per contract from the close mid quote (Black-Scholes,
  bisection -- monotone in vol, no derivative pathologies); contracts
  whose mid violates arbitrage bounds or that fail to converge are
  dropped and counted, never guessed.
- Spot is parity-implied from the chain itself (the strike minimizing
  |call_mid - put_mid|, plus the parity residual): self-consistent with
  the quotes, no extra data dependency.
- gex_i = gamma_i * OI_i * 100 * S * sign_i, summed over the chain
  (dollar gamma per 1.0 move in S; the per-1% variant is sweep-level).
- ZeroFlip: gamma is recomputed across a spot grid holding each
  contract's IV fixed; the flip is the first sign crossing of NetGEX(S').

Day-type classification (spec 2.3), calibrated on the trailing NetGEX
distribution -- no magic constants:

  NEG:     NetGEX < 0            or prior close below ZeroFlip
  POS:     NetGEX >= p66 of trailing distribution and close above flip
  NEUTRAL: everything else, and ALWAYS the fallback on missing/stale data
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class ChainEntry:
    """One option as the snapshot job hands it over."""

    strike: float
    expiry_days: float  # calendar days to expiry, can be fractional
    is_call: bool
    open_interest: int
    mid: float  # close mid quote


@dataclass(frozen=True)
class GexResult:
    net_gex: float
    zero_flip: float | None
    spot: float
    contracts_used: int
    contracts_dropped: int


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    spot: float, strike: float, t_years: float, vol: float, is_call: bool,
    r: float, q: float,
) -> float:
    if t_years <= 0 or vol <= 0:
        intrinsic = max(0.0, (spot - strike) if is_call else (strike - spot))
        return intrinsic
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t_years) / (
        vol * math.sqrt(t_years)
    )
    d2 = d1 - vol * math.sqrt(t_years)
    if is_call:
        return spot * math.exp(-q * t_years) * _norm_cdf(d1) - strike * math.exp(
            -r * t_years
        ) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * math.exp(
        -q * t_years
    ) * _norm_cdf(-d1)


def bs_gamma(
    spot: float, strike: float, t_years: float, vol: float, r: float, q: float
) -> float:
    """Identical for calls and puts."""
    if t_years <= 0 or vol <= 0 or spot <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t_years) / (
        vol * math.sqrt(t_years)
    )
    return math.exp(-q * t_years) * _norm_pdf(d1) / (spot * vol * math.sqrt(t_years))


def implied_vol(
    price: float, spot: float, strike: float, t_years: float, is_call: bool,
    r: float, q: float,
) -> float | None:
    """Bisection on vol in [1e-4, 5.0]; None when the mid violates bounds
    or the solver cannot bracket. Dropped contracts are counted upstream."""
    if t_years <= 0 or price <= 0:
        return None
    low, high = 1e-4, 5.0
    p_low = bs_price(spot, strike, t_years, low, is_call, r, q)
    p_high = bs_price(spot, strike, t_years, high, is_call, r, q)
    if not (p_low <= price <= p_high):
        return None
    for _ in range(80):
        mid = 0.5 * (low + high)
        p_mid = bs_price(spot, strike, t_years, mid, is_call, r, q)
        if abs(p_mid - price) < 1e-7:
            return mid
        if p_mid < price:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def parity_spot(chain: list[ChainEntry], r: float, q: float) -> float | None:
    """Spot implied by put-call parity at the most balanced strike of the
    nearest standard expiry (>= 5 days out, to dodge 0DTE noise)."""
    by_key: dict[tuple[float, float], dict[bool, float]] = {}
    for entry in chain:
        by_key.setdefault((entry.expiry_days, entry.strike), {})[entry.is_call] = entry.mid
    candidates = [
        (expiry, strike, sides)
        for (expiry, strike), sides in by_key.items()
        if len(sides) == 2 and expiry >= 5
    ]
    if not candidates:
        return None
    expiry = min(c[0] for c in candidates)
    at_expiry = [c for c in candidates if c[0] == expiry]
    balanced = min(at_expiry, key=lambda c: abs(c[2][True] - c[2][False]))
    _, strike, sides = balanced
    t = expiry / DAYS_PER_YEAR
    # C - P = S e^{-qT} - K e^{-rT}  ->  S = (C - P + K e^{-rT}) e^{qT}
    return (sides[True] - sides[False] + strike * math.exp(-r * t)) * math.exp(q * t)


def compute_gex(
    chain: list[ChainEntry],
    *,
    r: float = 0.04,
    q: float = 0.006,
    dealer_sign_calls: int = 1,
    min_expiry_days: float = 0.0,
    spot_override: float | None = None,
) -> GexResult | None:
    """NetGEX and ZeroFlip for one snapshot.

    `dealer_sign_calls=+1` is the standard convention (+calls/-puts);
    pass -1 for the inverse-convention robustness sweep.
    `min_expiry_days=1.0` excludes 0DTE (a declared sweep).
    """
    spot = spot_override if spot_override is not None else parity_spot(chain, r, q)
    if spot is None or spot <= 0:
        return None

    solved: list[tuple[ChainEntry, float]] = []
    dropped = 0
    for entry in chain:
        if entry.open_interest <= 0 or entry.expiry_days < min_expiry_days:
            continue
        vol = implied_vol(
            entry.mid, spot, entry.strike, entry.expiry_days / DAYS_PER_YEAR,
            entry.is_call, r, q,
        )
        if vol is None:
            dropped += 1
            continue
        solved.append((entry, vol))
    if not solved:
        return None

    def net_at(s: float) -> float:
        total = 0.0
        for entry, vol in solved:
            sign = dealer_sign_calls if entry.is_call else -dealer_sign_calls
            gamma = bs_gamma(s, entry.strike, entry.expiry_days / DAYS_PER_YEAR, vol, r, q)
            total += gamma * entry.open_interest * 100.0 * s * sign
        return total

    net = net_at(spot)

    # ZeroFlip: scan +/-6% in 0.25% steps for the crossing nearest spot.
    flip: float | None = None
    grid = [spot * (1 + step / 400.0) for step in range(-24, 25)]
    values = [(s, net_at(s)) for s in grid]
    crossings = [
        (a_s, b_s)
        for (a_s, a_v), (b_s, b_v) in zip(values, values[1:], strict=False)
        if a_v == 0 or (a_v < 0) != (b_v < 0)
    ]
    if crossings:
        nearest = min(crossings, key=lambda c: abs(0.5 * (c[0] + c[1]) - spot))
        flip = 0.5 * (nearest[0] + nearest[1])

    return GexResult(
        net_gex=net,
        zero_flip=flip,
        spot=spot,
        contracts_used=len(solved),
        contracts_dropped=dropped,
    )


def classify_day(
    net_gex: float,
    zero_flip: float | None,
    prior_close: float,
    trailing_net_gex: list[float],
    *,
    pos_percentile: int = 66,
    min_history: int = 40,
) -> str:
    """NEG / POS / NEUTRAL per spec 2.3. NEUTRAL is the only fallback:
    missing history or missing flip never guesses NEG (never size UP on
    missing data -- spec section 4)."""
    below_flip = zero_flip is not None and prior_close < zero_flip
    if net_gex < 0 or below_flip:
        return "NEG"
    if len(trailing_net_gex) < min_history:
        return "NEUTRAL"
    ordered = sorted(trailing_net_gex)
    rank = max(1, -(-len(ordered) * pos_percentile // 100))
    threshold = ordered[rank - 1]
    above_flip = zero_flip is None or prior_close > zero_flip
    if net_gex >= threshold and above_flip:
        return "POS"
    return "NEUTRAL"
