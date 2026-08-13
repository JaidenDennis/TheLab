# NAIM — Noise-Area Intraday Momentum: Final Report

**Status: DEAD, holdout-confirmed (2026-08-13). The declared variant
(`core`, the published replication's own configuration) went from
+$206/trade on develop to −$157/trade, PF 0.82, on the sealed post-
publication holdout — failing the pre-registered gate (≥ +$20/trade,
PF ≥ 1.10) on both counts. The mechanism reproduced, passed every develop
check, showed a genuine parameter plateau — and still died out of sample.
This is the cleanest crowding-decay result this repo has produced.**

| | |
|---|---|
| Strategy | NAIM v1 (`src/nq_agent/strategy/naim.py`), spec v1.0 |
| Mechanism source | Zarattini/Aziz/Barbon 2024 (SSRN 4824172) + independent NQ replication; Gao et al. 2018 for the hold-to-close posture |
| Data | NQ.v.0 1m fixtures, extended window, 2020-01-02 → 2026-08-12 |
| Noise curves | `scripts/precompute_noise.py`, trailing-L per-minute sigma, zero lookahead |
| Develop window | 2020-07-01 → 2024-09-30 (~1,070 sessions; identical for every variant) |
| Holdout | 2024-10-01 → 2026-08-12 — declared and read ONCE (declaration committed first: `NAIM-HOLDOUT-DECLARATION.md`, commit `1d5a1c4`-era; run immediately after) |
| Sizing / costs | 1 NQ contract flat; $10/RT + 1-tick adverse entry fill (divide by 10 for MNQ) |
| Calendar | FOMC 2020–2026 populated (2021–2026 verified against federalreserve.gov) |
| Run date | 2026-08-13 |

---

## 1. Verdict

- **The implementation is validated**: the §9.4 sanity anchor reproduced
  the published NQ replication's shape and magnitude on this engine —
  win 40.1% (published ~38%), payoff 2.19 (published ~2.25), EV +$206/trade
  (published ≈ +6 bps ≈ $180). The machinery is not the story.
- **The develop-window edge was real and robust**: 5 of 5 years positive,
  and a broad plateau — EV $165–$248 across lookbacks {14…120}, stop
  construction, calendar handling, entry-window end, and attempt caps.
- **The holdout killed it anyway**: −$157.19/trade over 474 trades,
  PF 0.82, win rate down 11 points, max drawdown 4,790 pts (7× develop).
  The holdout is entirely post-publication data; the edge did not survive
  becoming public.
- **Decay was immediate and monotone**: Q4-2024 −$0.34/trade (flat),
  2025 −$152, 2026 −$221. Not one bad regime — a progressive
  disappearance, which is what arbitrage looks like.

---

## 2. Sanity anchor (spec §9.4) — passed

| check | published NQ replication | this engine |
|---|---|---|
| Win rate | ~35–40% | 40.1% |
| Payoff (avg win / avg loss) | ~2.25 | 2.19 (83.6 / 38.2 pts) |
| Net EV/trade | ≈ +6 bps ≈ $180 | +$206.23 |
| Profitable | Sharpe ≈ 1.67 | +$189,730, PF 1.48 |

Develop gate (pre-registered §9.2): EV ≥ $40 ✓ (+$206) · ≥3/4 years
positive ✓ (5/5: +$239 / +$186 / +$288 / +$76 / +$242 EV by year 2020–24)
· PF ≥ 1.15 ✓ (1.48) · ≥8 trades/month ✓ (18.0). **PASSED.**

---

## 3. The variant matrix (develop, descriptive)

All 15 cells pre-declared in `scripts/run_naim.py` before any result
existed. The holdout variant was declared before any cell beyond `core`
had been seen, so nothing below is contaminated by selection — and nothing
below may claim the (now spent) holdout either.

| variant | trades | net $ | EV/trade | win% | PF | maxDD pts |
|---|---|---|---|---|---|---|
| **core** (anchor: L90, 30m, no gate) | 920 | 189,730 | **+206.23** | 40.1% | 1.48 | 692 |
| core_1m | 2,006 | 30,805 | +15.36 | 32.6% | 1.06 | 2,159 |
| core_L14 | 965 | 179,290 | +185.79 | 40.1% | 1.45 | 880 |
| core_L30 | 944 | 176,355 | +186.82 | 40.0% | 1.44 | 761 |
| core_L60 | 935 | 172,780 | +184.79 | 39.4% | 1.43 | 767 |
| core_L120 | 908 | 192,845 | +212.38 | 40.2% | 1.49 | 719 |
| core_touch | 976 | 196,775 | +201.61 | 39.9% | 1.51 | 756 |
| core_novwap | 708 | 175,420 | +247.77 | 47.7% | 1.36 | 984 |
| core_gate (+OFI) | 450 | 74,225 | +164.94 | 44.9% | 1.33 | 899 |
| core_1m_gate | 1,634 | 77,805 | +47.62 | 33.6% | 1.13 | 1,785 |
| core_nocal | 935 | 188,690 | +201.81 | 40.0% | 1.46 | 644 |
| core_end13 | 777 | 168,280 | +216.58 | 38.6% | 1.49 | 741 |
| core_end14 | 851 | 166,125 | +195.21 | 38.9% | 1.44 | 751 |
| core_cap2 | 829 | 169,350 | +204.28 | 39.6% | 1.46 | 930 |
| core_cap6 | 926 | 186,000 | +200.86 | 40.0% | 1.46 | 694 |

What the matrix measured:

1. **The 30-minute boundary cadence is load-bearing.** Same bands, same
   stops, evaluated every minute instead of at HH:00/HH:30: EV collapses
   from +$206 to +$15 and trades double. The noise band's edge lived in
   *disciplined evaluation timing*, not in the band alone.
2. **A true parameter plateau everywhere else** — L, stop mode, calendar,
   window end, caps: all within ±20% of the anchor. This was not a
   curve-fit; it was a broad, real develop-era edge.
3. **The OFI gate SUBTRACTS here** (+$165 vs +$206, and total profit
   halved): the spec's "must re-earn its value, no credit from SME" rule
   was vindicated — same component, different mechanism, opposite sign.
4. **FOMC calendar ≈ worth $4/trade on develop** (core vs core_nocal) —
   the ablation SME never ran, now measured: small, not decisive.

---

## 4. The holdout read (one shot, declared first)

Declared variant `core`; declaration committed before the read
(`NAIM-HOLDOUT-DECLARATION.md`); seeded walk-forward from the develop
terminal state.

| | develop | holdout | gate | verdict |
|---|---|---|---|---|
| Net EV/trade | +$206.23 | **−$157.19** | ≥ +$20 | **FAIL** |
| Profit factor | 1.48 | **0.82** | ≥ 1.10 | **FAIL** |
| Net | +$189,730 | −$74,510 | | |
| Trades | 920 | 474 | | |
| Win rate | 40.1% | 29.1% | | |
| Avg win / loss (pts) | +83.6 / −38.2 | +115.4 / −57.8 | | |
| maxDD (pts, closed) | 692 | 4,790 | | |

Decay path: **Q4-2024: −$0.34/trade (n=59) → 2025: −$152 (n=250) →
2026: −$221 (n=165).** The edge was already gone the quarter after the
develop window ended and deteriorated monotonically from there.

### Post-mortem observations (descriptive ONLY — the holdout is spent;
none of this may select a v2)

- **The exit mix inverted**: catastrophic-stop exits went from 8% of
  trades (develop) to 34% (holdout). NQ roughly doubled over the sample,
  and the spec's fixed **80-point** catastrophic stop — the one component
  that deviates from the source paper, added for prop compliance — became
  proportionally half as wide while the noise band (fractional) scaled
  correctly. Some of the holdout loss is that geometry error; the win-rate
  collapse and monotone decay say most of it is the edge itself.
- Average win *grew* (115 pts) — the trades that worked still trended.
  There were simply far too few of them.

---

## 5. What survives NAIM

- **Process assets**: noise-curve precompute (zero-lookahead, any
  lookback), the pre-declaration pattern (declare → commit → read),
  and the sanity-anchor discipline (§9.4 caught nothing this time
  because the implementation was right — but it's the reason we know
  the failure is the market's, not the code's).
- **Measured knowledge**: evaluation cadence matters more than signal
  shape; the OFI gate is mechanism-specific, not a universal enhancer;
  FOMC filtering is worth ~$4/trade here; fixed-point stops silently
  decay as price levels rise — scale risk in fractions, not points.
- **The meta-finding, twice confirmed** (SME, now NAIM, on
  non-overlapping mechanisms): develop-era NQ intraday momentum edges —
  even robust, externally validated, plateau-wide ones — have not been
  surviving into 2025–26. Any successor thesis must either (a) not be a
  published/crowded pattern class, or (b) carry a mechanism for why
  crowding cannot arbitrage it (capacity, horizon, or structural flow).

## 6. Candidate directions (nothing here is endorsed by data yet)

1. A v2 with fraction-scaled catastrophic stops and 30m cadence is the
   obvious mechanical fix — but it has **no out-of-sample period left**.
   It could only be validated in shadow mode against forward data.
2. Full-OFI (true aggressor-side) microstructure signals on the tick
   year — unpublished territory, data already on disk, but the tick year
   overlaps the spent holdout period; treat as research, not validation.
3. Longer-horizon or capacity-constrained effects where crowding decay is
   structurally slower.

---

## 7. Reproduce

```
uv run python scripts/precompute_noise.py --fixtures var/fixtures/1m \
    --out var/noise --lookbacks 14 30 60 90 120

uv run python scripts/run_naim.py --fixtures var/fixtures/1m --noise var/noise \
    --start 2020-07-01 --end 2024-09-30 --out var/naim --variant all

# the (spent) holdout read, exactly as executed:
#   declaration: NAIM-HOLDOUT-DECLARATION.md (committed first)
#   seeded: cp var/naim/core/state.db var/naim-holdout/core/state.db
uv run python scripts/run_naim.py --fixtures var/fixtures/1m --noise var/noise \
    --start 2024-10-01 --end 2026-08-12 --out var/naim-holdout --variant core
```

Everything runs through the production engine; there is no separate
simulator to drift out of sync.
