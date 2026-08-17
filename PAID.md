# PAID — Post-Auction Imbalance Drift: Phase-1 Report (FMB-1.4)

**Status: NOT PASSED — the pre-registered gate fails on statistical
power (0 of the required 2 horizons reach T ≥ 2.0; n = 47 vs 50
minimum) — but this is the batch's only survivor-shaped result: the
bracket hit 42.6% (bar 42%), EV +$251/trade (t = 1.30), and the unwind
drift is monotone in horizon exactly as the inventory channel predicts.
Per the recorded prior, this is a "promising, not passed" in the FAR
sense: no promotion today, and the path forward is more data, not a
relaxed gate. Re-run: `uv run python scripts/fmb1_studies.py paid`.**

| | |
|---|---|
| Spec | FMB-1.4 (batch spec 2026-08-17); mechanism: close-auction inventory unwind |
| Data | NQ tick year 2025-08-13 → 2026-08-12 (249 sessions with close flow) + 1m fixtures for outcomes; all on disk, $0 |
| Geometry | Next-session one shot 09:35 ET in the unwind direction, fraction-rebased 100/50 bracket, backstop 120 min |
| Costs | $10/RT + 1-tick adverse entry + 1-tick adverse stop fill; adverse-first bar resolution |
| Machinery | `scripts/fmb1_studies.py paid` → `var/fmb1/paid.json`, close-flow series cached in `var/fmb1/paid_close_flow.json` |
| Run date | 2026-08-17 |

---

## 1. The close-imbalance series (15:50–16:00 ET signed aggressor flow)

- 249 sessions; mean imbalance **−0.72%** of close-window volume,
  median −0.72%, only 43% of sessions net-buy — the NQ close skews
  sell-side through the tick year (t = −3.06), consistent with GFM's
  put-heavy-chain picture of the same period.
- Qualifying days: |imbalance| ≥ trailing-60-session Q80 (walk-forward,
  ≥ 30 prior sessions required) → **47 qualifying days**, trade taken
  next session at 09:35 against the imbalance sign.
- Sides ('B' buy / 'A' sell aggressor) use the engine's own Databento
  convention; unknown-side ticks are dropped (declared — the ratio's
  sign is robust to the ~16% tick-rule-resolved residue).

## 2. Unwind-direction forward returns (the gate that failed)

Signed next-morning returns from the 09:35 open, unwind direction:

| horizon | mean | median | share > 0 | t |
|---|---|---|---|---|
| 15m | +1.6 bp | +4.3 bp | 57.4% | 0.30 |
| 30m | +2.0 bp | −0.7 bp | 48.9% | 0.28 |
| 60m | +11.3 bp | +20.5 bp | 59.6% | 1.38 |
| 120m | **+16.0 bp** | **+22.3 bp** | **68.1%** | **1.64** |

Monotone-increasing with horizon — the shape the overnight-inventory
literature predicts for a slow unwind — but the gate required T ≥ 2.0
at ≥ 2 horizons and the best cell reaches 1.64. **Gate: FAILED.** At
this effect size (~16 bp/120m), reaching T ≈ 2 needs roughly 70–90
qualifying days; one tick year supplies 47.

## 3. The bracket panel

| cell | n | hit | stop | time | EV/trade | t |
|---|---|---|---|---|---|---|
| all qualifying | 47 | **42.6%** | 55.3% | 2.1% | **+$251** | 1.30 |
| rebalance days (month/quarter-end) | 8 | 50.0% | 50.0% | 0% | +$481 | 0.93 |
| normal days | 39 | 41.0% | 56.4% | 2.6% | +$204 | 0.97 |

The only cell in the entire FMB-1 batch to clear the 42% hit-rate bar,
and the calendar split points the predicted way (index-rebalance
pressure is the bigger inventory event). Break-even is ≈ 34%; the
sample profit is +$11,809 per NQ contract over the year (÷10 for MNQ).

## 4. Why this is NOT a pass, in plain terms

1. **The pre-registered gate is failed on its face**: 0/2 horizons
   significant, n = 47 < 50. Promoting on the bracket cell alone would
   be selecting the best of eight-plus cells across a four-study batch
   — precisely the multiple-comparisons trap batch rule 3 exists for.
2. **One tick year, one regime.** The close-flow series is only
   measurable on the free trailing-year window; every number above
   shares whatever 2025-26 idiosyncrasy TFR's develop year has.
3. **t = 1.30 on EV** is a coin that has come up nicely, not evidence.

## 5. The recorded path forward (a decision, not an assumption)

Per the spec's data note, a Phase-1 **pass** was to motivate the
develop-era tick purchase (~$250–300, 2021-2024) as confirmation.
Phase 1 did **not** pass, so that purchase is *not* motivated today.
Two $0 alternatives are recorded instead:

- **Accrue forward**: the free tick window trails forward daily; at
  the observed qualifying rate (~4/month) the n ≥ 50 floor arrives in
  ~1 month and the ~80-day power estimate in ~8–9 months. Re-running
  this exact study then is $0 and touches no budget.
- **Re-quote the historical pull** only if the accrued result holds
  its shape at n ≥ 60 — at which point the ~$250–300 becomes a
  confirmation of a measured effect rather than a fishing license.

## 6. Verdict

**Not passed; not dead.** PAID is the only FMB-1 mechanism whose
Phase-1 evidence points the predicted way on both the drift and the
bracket. It earns a diary entry and a $0 re-run date, not a program:
no strategy module, no cycles, no shadow slot, and the tick purchase
stays unbought until the effect survives a larger n.
