# TFR — Tick-Flow Regime: Cycle-1 Report (v1.1 "Flow Core")

**Status: DEVELOP GATE PASSED — first strategy in this program to clear
every pre-registered bar, including T ≥ 2.0 and the permutation control.
Declared variant (by pre-registered rule): `fc_t13` — Flow Core entries,
13-bar time exit. Next step: the shadow-forward gate (spec §9.3), which
requires the live harness to be built and remains the ONLY path to
funding. Nothing here authorizes live trading.**

| | |
|---|---|
| Strategy | TFR v1.1 (`src/nq_agent/strategy/tfr.py`), spec v1.0 + cycle-1 amendment |
| Cycle accounting | Research cycle **1 of 2 spent** (Option A: follow the control). Cycle 2 reserved, unscoped |
| Data | Free trailing tick year only (user direction, **$0 data cost**): 2025-08-13 → 2026-08-12, 258 sessions; aggressor-side verified (median tick-rule agreement 84.3%, 0 sessions excluded) |
| Window caveats | Single year → quarter-stability substitutes for year-stability; window overlaps the spent SME/NAIM holdout (soft contamination, disclosed); the 2021-2024 tick purchase in the amendment was superseded by user direction |
| Costs | $10/RT + 1-tick adverse entry; 1 NQ contract flat (divide by 10 for MNQ) |
| Run date | 2026-08-13 |

---

## 1. The develop gate — PASSED (all seven bars)

Declared variant `fc_t13`: flow-threshold entries (|F1_5| ≥ trailing Q70,
vol_z ≥ 0.5, cap 3/day, 09:35–15:00, FOMC block), 13-bar (65 min) time
exit, 0.35%-of-price catastrophic stop, 15:55 flatten. Regime layer:
annotations only (an invariant test proves regime fields cannot reach a
trading decision).

| bar | required | fc_t13 | verdict |
|---|---|---|---|
| Net EV/trade | ≥ +$40 | **+$306.93** | ✓ |
| T-statistic | ≥ 2.0 | **2.58** | ✓ |
| N | ≥ 100 | 307 | ✓ |
| Profit factor | ≥ 1.15 | 1.49 | ✓ |
| Trades/month (median) | ≥ 8 | 31 | ✓ |
| Quarter stability | ≥3 of 4 positive | **4 of 4** (+$409 / +$309 / +$206 / +$427 EV) | ✓ |
| Permutation | p < 0.05 | **p ≈ 0.048** (beat all 20 scrambles) | ✓ |
| Beats V-T13 baseline | required | is V-T13, by the selection rule (§3) | ✓ |

Headline economics (develop, per contract): net +$94,228/year ≈
$7,850/month NQ ≈ **$785/month per MNQ micro**; median hold 63 minutes;
max closed-trade drawdown 1,047 points (≈ $2,095/micro against a
$4,500-class trailing limit; intra-trade is worse — shadow will measure).

## 2. The permutation control — the result that matters most

20 full-pipeline reruns with aggressor signs scrambled per minute
(magnitudes preserved, direction destroyed). Trade counts stay ~310
(entries key on |F1| magnitude), but the money disappears:

- Permuted EVs: −$205 … +$165, mean ≈ +$23, all 20 below the real +$307
- **The direction of the flow is worth ≈ $280/trade.** This edge is not
  "volatility moments are good times to trade"; it is specifically the
  informed-flow direction — the Cont/Kukanov/Stoikov mechanism, measured
  in our own data.

## 3. The exit matrix — and the third confirmation of the hold-duration law

All 14 pre-declared v1.1 cells, identical entries where comparable:

| variant | trades | net $ | EV/trade | win% | PF | maxDD pts |
|---|---|---|---|---|---|---|
| **fc_t13 (baseline → declared)** | **307** | **94,228** | **+306.93** | **48.2%** | **1.49** | **1,047** |
| fc_fd (flow decay) | 349 | 78,453 | +224.79 | 46.7% | 1.45 | 598 |
| fc_hf (hostile flow) | 327 | 67,152 | +205.36 | 43.1% | 1.34 | 1,363 |
| fc_fstack | 360 | 67,730 | +188.14 | 45.0% | 1.40 | 778 |
| fc_fd_h1 (1-bar hysteresis) | 395 | 39,246 | +99.36 | 38.7% | 1.30 | 678 |
| fc_fstack_q60 | 422 | 100,627 | +238.45 | 44.8% | 1.48 | 884 |
| fc_fstack_q80 | 249 | 48,927 | +196.49 | 45.4% | 1.45 | 563 |
| fc_fstack_v0 | 486 | 59,788 | +123.02 | 41.4% | 1.28 | 798 |
| fc_fstack_v10 | 216 | 74,160 | +343.34 | 45.8% | 1.69 | 607 |
| fc_fstack_qhf55 | 367 | 69,966 | +190.64 | 43.6% | 1.42 | 816 |
| fc_fstack_qhf85 | 352 | 86,046 | +244.45 | 47.2% | 1.51 | 598 |
| fc_fstack_f4 / f3 | 360/357 | ≈67.5k | ≈+188 | 45% | 1.40 | 778 |
| fc_fstack_cap5 | 384 | 87,614 | +228.16 | 46.1% | 1.50 | 575 |

Readings, in order of importance:

1. **Every reactive flow exit lost to the dumb 13-bar clock** — the §6
   catch condition triggered exactly as designed, and the report is
   obliged to say it plainly: *hold duration, not exit intelligence,
   carries the value.* This is now measured three times in this program
   (SME's trail ablation, NAIM's cadence collapse, TFR's exit matrix) on
   three unrelated mechanisms. Tightening the 1-bar decay hysteresis made
   it worse ($99), loosening thresholds toward "harder to shake out"
   (qhf85) made stacks better — the gradient points at T13 from every
   direction. The selection rule put the baseline on the throne without
   any human judgment call.
2. **Sweeps are a plateau, not a spike**: every cell positive, EV $99–$343.
   The vol_z=1.0 cell ($343, PF 1.69) is the strongest single cell but was
   a stack-family sweep, not a declared candidate — recorded as cycle-2
   material, not selected (no post-hoc blends, per the amendment).
3. F4 confirmation and F3 veto: negligible at these thresholds (F4
   changed nothing; F3 removed 3 trades). Cut per the must-add-EV rule.
4. All-cells context: v1.0's full-regime stack produced 33 trades in one
   anomalous month over the same window (v1.0 report, superseded by this
   one, is preserved in git history at `5b22185`).

## 4. Honest limits of this result

- **One year of data, chosen for being free.** No 2021–2024 evidence
  exists at tick resolution; year-over-year robustness is untested by
  construction. The quarter table is the strongest stability statement
  the window allows.
- **The window overlaps the spent SME/NAIM holdout** (soft contamination:
  the designer had seen aggregate results from that period, though no TFR
  rule derives from any holdout slice).
- The T13 exit means positions ignore flow for 65 minutes; the
  catastrophic stop (0.35%) is the only intra-hold protection. The
  closed-trade drawdown understates intra-trade excursion; the shadow
  phase measures the true figure.
- Fill model: 5m-close + 1 tick on market entries; real slippage on NQ
  market orders at these sizes should sit inside the $10/RT + 1-tick
  budget, but the shadow's execution-drift audit is the proof.

## 5. Next step: the shadow-forward gate (spec §9.3, unchanged)

**Declaration draft** (becomes binding when committed as
`TFR-SHADOW-DECLARATION.md` on the spec owner's sign-off):
variant `fc_t13` exactly as specified in §1 — no parameter changes
between now and the end of the shadow window.

Shadow gate: live-paper through the production engine, real-time data,
**3 months or 60 trades, whichever is longer**; pass = net EV ≥
+$20/trade, PF ≥ 1.10, execution drift ≤ 1 tick median. Pass → smallest
funded tier. Fail → dead, no parameter rescue against shadow data.

**Build list for the shadow harness** (the remaining engineering):

1. Live tick ingestion: `DatabentoFeed` first real connection (never yet
   connected live; plan includes live data) plus a tick→feature stream
   that computes F1/vol_z in real time — the decision-file values,
   computed live. The offline pipeline is the reference implementation
   the live one must match bar-for-bar (drift audit #1).
2. Rolling calibration jobs (Q tables, z-scores) running nightly from
   the journal, exactly as the walk-forward did.
3. Paper executor + the existing risk governor, journal, and reconcile
   layers — already built and tested; this is wiring, not invention.
4. The drift audit itself: per-bar comparison of live-computed features
   vs the offline recomputation, journaled.

## 6. Reproduce

```
uv run python scripts/precompute_flow.py --ticks var/fixtures/trades --out var/flow
uv run python scripts/fit_regimes.py --flow var/flow --out var/decisions/k3m --k 3 --refit monthly
for v in fc_hf fc_fd fc_fstack fc_t13 ...; do
  uv run python scripts/run_tfr.py --fixtures var/fixtures/1m \
      --decisions var/decisions/k3m --out var/tfr --variant $v
done
# permutation control (20 seeds):
uv run python scripts/permute_flow.py --flow var/flow --out <tmp> --seed N
# then refit + rerun fc_t13 per seed; observed EV must beat all seeds.
```

Variant definitions: `scripts/run_tfr.py::VARIANTS`. Everything runs
through the production engine; the strategy runtime is pure logic over
walk-forward decision files, which is exactly what the live harness must
reproduce.
