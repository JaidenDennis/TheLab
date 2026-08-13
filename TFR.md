# TFR — Tick-Flow Regime: Develop Report (v1.0 as specified)

**Status: develop gate FAILED for v1.0 as specified — but the program is
alive.** The regime layer, not the flow signal, is what failed: the
pre-declared regime-off control found the tick-flow signal itself positive
in **all four quarters** at 42 trades/month (T = 1.53, shy of the 2.0 bar,
with a known exit handicap — §6). Both of the spec's §9.5 research cycles
remain unspent. **No shadow declaration has been made; deciding how to
spend cycle 1 is the next decision, and it belongs to the spec owner.**

| | |
|---|---|
| Strategy | TFR v1 (`src/nq_agent/strategy/tfr.py`), spec v1.0 |
| Data | Free trailing tick year only (user direction, $0): 2025-08-13 → 2026-08-12, 258 sessions, aggressor-side trades + existing 1m fixtures |
| Data QA | 0 sessions excluded; unknown-side ≈ 0%; median tick-rule agreement 84.3% (side convention verified) |
| Pipeline | ticks → per-minute flow aggregates → walk-forward decision files (rolling z-scores, trailing percentiles, monthly GMM K=3 with deterministic label map, Markov transitions, Mahalanobis health cut) → strategy runtime does no statistics |
| Models | 10 monthly refits; 200 of 258 sessions carry a fitted model (the rest are warmup) |
| Costs | $10/RT + 1-tick adverse entry; 1 contract flat, NQ-equivalent |
| Protocol adaptations | Year-stability → quarter-stability (12.5-month window); shadow-forward remains the true gate per spec §9. This window overlaps the spent SME/NAIM holdout (soft contamination, disclosed per §9.3) |
| Run date | 2026-08-13 |

---

## 1. The develop gate, v1.0 as specified — FAIL

Declared candidate universe: V-HF / V-RI / V-STACK with the full regime
stack. Best cell (V-HF by net; V-STACK by PF):

| criterion | bar | best full-stack cell | verdict |
|---|---|---|---|
| Net EV/trade | ≥ +$40 | +$433 to +$467 | ✓ |
| T-statistic | ≥ 2.0 | **0.66** | **✗** |
| PF | ≥ 1.15 | 1.67–1.72 | ✓ |
| Trades/month (median) | ≥ 8 | **≈ 2.6, and 0 after Nov 2025** | **✗** |
| Quarter stability | ≥3 of 4 positive | **un-measurable: all trades in one quarter** | **✗** |
| Beats V-T13 | required | ✓ ($433–467 vs $334) | ✓ |
| Permutation p < 0.05 | required | not run — moot below frequency gate | — |

## 2. Why: the arming layer is structurally broken for rare states

Monthly arming funnel (decision files, defaults p_arm = 0.60, vol_z ≥ 0.5):

| month | sessions w/ model | AF bars | armed | qualified |
|---|---|---|---|---|
| 2025-11 | 20 | **615** | **409** | **55** |
| 2025-12 | 22 | 140 | 0 | 0 |
| 2026-01…08 | 158 | 0–192/mo | **0** | **0** |

Two compounding defects:

1. **Refit instability.** The November model labeled 39% of bars
   Active-Flow — a "tail state" that covered a third of the month. Every
   other model kept AF at 0–8%. The deterministic centroid mapping is
   deterministic per fit, but the *clusters themselves* swing between
   monthly fits.
2. **The transition-probability bar is hostile to rare states by
   construction.** A state with a 5% base rate cannot show 0.60 trailing
   retention on a 200-bar estimate unless it blankets the tape (which is
   exactly the November anomaly). `t_af ≥ 0.60` therefore armed only the
   month the model misbehaved — the gate selected FOR the failure mode.

All 33 full-stack trades sit inside 2025Q4. The +$433 EV is real money in
the journal but has no statistical standing (T = 0.66, one active month).

Also recorded: at default thresholds the `p_arm=0.45`, `f4_confirm`, and
`f3_veto` cells were bit-identical to the base stack — those conditions
never bound on this window. And the first build of the `no_regime` control
was mis-built (it still required the AF label); it was fixed
(`regime_required=False`) and rerun before this report — an
instrumentation fix, not a research cycle.

## 3. The full matrix (12 pre-declared cells + corrected control)

| variant | trades | net $ | EV/trade | win% | PF | maxDD pts |
|---|---|---|---|---|---|---|
| exit_hf | 32 | 14,959 | +467.46 | 40.6% | 1.67 | 278 |
| exit_ri | 32 | 12,809 | +400.29 | 43.8% | 1.63 | 389 |
| exit_stack | 33 | 14,292 | +433.08 | 48.5% | 1.72 | 369 |
| exit_t13 (baseline) | 32 | 10,679 | +333.73 | 40.6% | 1.47 | 251 |
| stack_parm75 | 30 | 14,447 | +481.55 | 50.0% | 1.75 | 369 |
| stack_q60 | 43 | 20,043 | +466.12 | 44.2% | 1.75 | 587 |
| stack_q80 | 19 | 14,483 | +762.26 | 47.4% | 2.37 | 269 |
| stack_qhf85 | 32 | 13,202 | +412.55 | 43.8% | 1.66 | 369 |
| parm45 / f4 / f3 | 33 | — | identical to exit_stack (conditions never bound) | | | |
| **no_regime (control, corrected)** | **397** | **55,064** | **+138.70** | **48.9%** | **1.38** | **732** |

Exit-matrix reading (thin sample, one quarter): all three state exits beat
the T13 baseline; median holds 22–25 min; V-STACK attribution split
evenly (12 hostile-flow / 12 regime-invalidation). The spec's §6 catch
condition (fast holds AND losing to T13) did not trigger — but nothing
here has statistical standing at N ≈ 33.

## 4. The headline finding: the flow signal works; the regime layer starves it

The corrected control — flow-threshold entries (|F1_5| ≥ Q70, vol_z ≥ 0.5),
no GMM anywhere — over the same window:

| | no_regime control |
|---|---|
| Trades | 397 (median 42/month — frequency gate ✓) |
| Net / EV | +$55,064 / **+$138.70 per trade** |
| T-statistic | **1.53** (bar: 2.0) |
| PF / win% | 1.38 / 48.9% |
| Avg win / loss | +$1,101 / −$772 |
| Quarters | **4 of 4 positive**: Q4 +$275, Q1 +$35, Q2 +$156, Q3 +$153 EV |

**Known handicap in this cell:** with the regime bypassed at entry, the
V-STACK's regime-invalidation exit degenerates — "not AF for 2 bars" is
almost always true, so 75% of exits fired at the 2-bar mark and median
hold collapsed to 10 minutes. The control is, accidentally, a
flow-burst-hold-10-minutes strategy. That it still clears +$139/trade in
all four quarters *despite* an exit stack working against it is the
strongest evidence in this report. It is NOT a validated strategy: T = 1.53
is below the bar, and its exits were never designed.

## 5. Where this leaves the program (decision required)

Spec §9.5 allows **two** research cycles before the feature set locks.
None is spent. The evidence points at one obvious cycle-1 scope, but with
two slots for the whole program, the spec owner chooses:

- **Option A — follow the control:** drop the GMM regime layer (or demote
  it to a journal annotation), keep the flow-threshold entry, and design
  the exits properly for it (V-HF and V-T13 already exist and are
  regime-independent; the RI family needs a flow-native replacement).
  Rationale: measured, all-quarter-positive, right frequency; the layer
  the cycle removes is the one the control showed subtracts ~90% of
  opportunity while adding model risk.
- **Option B — rebuild the arming layer:** keep the regime thesis, fix
  what §2 diagnosed (posterior-probability arming instead of trailing
  transition estimates; a label-stability constraint across refits;
  longer fit windows). Rationale: the Mesfin positive-control shape used
  the regime layer; November's $433/trade hints at what a stable version
  might select. Costs the harder engineering and keeps model risk.
- **Doing both is two cycles** — the whole budget.

Permutation controls (§10.9) run against whichever cycle-1 candidate
emerges, before any shadow declaration. The shadow-forward gate
(§9.3) remains unchanged and is the only path to funding either way.

## 6. Reproduce

```
uv run python scripts/precompute_flow.py --ticks var/fixtures/trades --out var/flow
uv run python scripts/fit_regimes.py --flow var/flow --out var/decisions/k3m --k 3 --refit monthly
uv run python scripts/run_tfr.py --fixtures var/fixtures/1m \
    --decisions var/decisions/k3m --out var/tfr --variant all
```

Variant definitions: `scripts/run_tfr.py::VARIANTS`. Everything runs
through the production engine; the strategy runtime is pure logic over the
decision files, which is also what the shadow harness will compute live.
