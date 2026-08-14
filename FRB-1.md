# FRB-1 — Flow Research Batch 1: Five Studies, One Report

**Scoreboard: one promising-but-not-passed (FAR), two clean nulls (VCT,
DDR), two significant INVERSIONS recorded as anomalies, not promoted
(SSF, LL-overlay). Zero protocol budget spent; zero strategy modules
built; TFR's last research cycle remains unspent, with FAR the only
candidate left standing in the funnel — and it needs a redesign before
it deserves the slot. Total data cost of the batch: $0 (LL ran on the
FC-ES data already purchased under program 6). Per batch rule 3, all
five outcomes are reported together so the denominator is visible: five
studies, one journal, and the significant results include two that point
the WRONG way — exactly the base-rate warning that rule exists to give.**

| | |
|---|---|
| Data | NQ tick year (existing flow/decision files) + ES flow (program 6's pull); fc_t13 develop journal (307 trades) |
| Method | Overlay-first: walk-forward feature series, column-joins on trades that already happened, pre-registered gates |
| Machinery | `scripts/frb1_studies.py` (far / vct / ssf / ddr / ll) |
| Run date | 2026-08-14 |

---

## FRB-1.1 — LL: ES→NQ Flow Lead-Lag — **KILL (by pre-registered condition)**

- **Cross-correlation panel** (minute resolution — the data's floor; the
  spec's sub-second aggregations would need tick re-parsing): ES flow vs
  forward NQ returns is slightly **negative** at every (window, lag) cell
  tested — mean corr −0.005 to −0.018, positive in only 2–6 of 13 months.
  No stable lead exists at horizons this data can see.
- **Overlay REVERSED**: fc_t13 entries with ES-flow agreement earned
  $230/trade (n=275); entries where ES **disagreed** earned **$965/trade
  (n=32, p = 0.048)** — large, significant, and opposite the prediction.
- Verdict per the spec's own kill condition ("no stable lead, or the
  overlay split reverses"): **killed; ES flow demoted to FC-ES-only
  use.** The reversal is recorded as an anomaly (possible reading:
  NQ-idiosyncratic flow unconfirmed by ES is the *most* informative
  flow), but n = 32 inside a five-study batch is an invitation to
  overfit, not evidence. Revisiting it costs a new spec.

## FRB-1.2 — FAR: Signed-Flow Autocorrelation Regime — **PROMISING, NOT PASSED**

EV of fc_t13 entries by FAR tercile (walk-forward percentile of the AR(1)
of signed 1m flow):

| window | BOT | MID | TOP | TOP−BOT | p | trades tagged |
|---|---|---|---|---|---|---|
| 60m | $348 | $533 | $514 | +$166 | 0.357 | 124 |
| **90m** | **−$161** | $463 | **$884** | **+$1,045** | **0.019** | 86 |
| 180m | −$435 | $1,081 | $874 | +$1,309 | 0.013 | 33 |

- The gradient is large, monotone-ish, in the predicted direction, and
  strengthens with window length — but the AR window structurally
  excludes morning entries (90m+ of same-session history required), so
  no significant cell reaches the n ≥ 60/bucket bar, and the tagged
  subsample (afternoon entries only) is not a random draw of the journal.
- **Verdict: the only surviving cycle-2 candidate, conditional on a
  redesign** (cross-session AR seeding or a shorter-lag formulation that
  can tag morning entries). The gate as pre-registered is NOT passed;
  promoting it today would be spending TFR's last cycle on a confounded
  measurement.

## FRB-1.3 — DDR: Delta-Divergence Reversal — **STAGE-1 FAIL**

Forward returns against the divergence direction: only one (threshold,
horizon) cell reaches T ≥ 1.5 (+3.3 pts at 6 bars, T = 1.82, |div| ≥ 1.0)
vs the two horizons required; at |div| ≥ 1.5 nothing is significant. Even
the best cell's gross (≈$66 NQ) barely clears round-trip friction.
**No stage 2; the reversal-side family stays closed** (consistent with
AFR's same-day death — see AFR.md).

## FRB-1.4 — SSF: Size-Split Flow — **FAIL, WITH A SIGNIFICANT INVERSION**

1,764 big-vs-small disagreement events: forward returns in the BIG-lot
direction are **negative** at every horizon (−2.9 pts at 6 bars,
**T = −2.12**). "Side with size" is wrong on this tape; at disagreements,
small lots had it right. The pre-registered gate (big-lot predictive)
fails; the inversion is recorded, not promoted — same discipline as LL's
reversal, same multiple-comparisons caveat. F4's earlier null
generalizes: size split carries no *usable* marginal signal in the
predicted direction. (Entry overlay not computed — join defect in the
study script, noted; immaterial to the verdict given the panel.)

## FRB-1.5 — VCT: Volume-Clock Toxicity — **NULL**

Tercile EV gradients lean positive (+$213 top-vs-bottom both intraday
and day-open) but non-monotone (MID highest) with p ≈ 0.23–0.28 — well
short of the gate. The gamma cross-tab shows VCT tiers spread roughly
evenly across the NEG-dominated year: it measures something different
from gamma, but not something that conditions fc_t13's EV at this
resolution. **Family closed at study stage.**

---

## Batch synthesis

1. **The funnel result**: of three cycle-2 candidates (LL, FAR, SSF),
   one survives (FAR) and only conditionally. Nothing is promoted today;
   TFR's last cycle stays banked.
2. **The base-rate lesson, made visible per rule 3**: five studies
   produced three "significant" splits (FAR +$1,045 predicted direction;
   LL −$735 reversed; SSF T = −2.12 reversed). One journal, five looks —
   significant reversals arriving at the same rate as significant
   confirmations is exactly what multiple comparisons on a shared sample
   looks like, and why every proceed-gate here is a promotion threshold,
   not proof.
3. **Convergent negative knowledge**: DDR + AFR (same day, independent
   designs) close the reversal side of NQ intraday flow. The tape pays
   continuation; it does not pay fading, however the fade is dressed.
4. All study fields remain advisory-only; nothing here touches the
   fc_t13 shadow or any trading path.

## Reproduce

```
uv run python scripts/frb1_studies.py far [--window 60|90|180]
uv run python scripts/frb1_studies.py vct
uv run python scripts/frb1_studies.py ssf
uv run python scripts/frb1_studies.py ddr
uv run python scripts/frb1_studies.py ll
```

Summaries land in var/frb1/*.json; every series is walk-forward from
var/flow, var/es-flow and var/decisions/k3m.
