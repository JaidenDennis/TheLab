# ODC — Overnight Drift Capture: Phase-1 Report (FMB-1.3)

**Status: KILLED at Phase 1 (2026-08-17), $0 spent — with a recorded
INVERSION. The overnight window's drift is real but tiny (+1.5 bp/
session, t = 1.04), the 100/50 first-passage bracket converts it to a
25.4% hit rate (kill line 36%), and the pre-registered cross-index
alignment conditioning is monotone in the WRONG direction (hit rate
falls from 28.6% to 20.9% as alignment "improves"). The geometry is
feasible (median window range 0.61% vs 0.33% target) — the mechanism,
not the ruler, failed. Re-run: `uv run python scripts/fmb1_studies.py
odc`.**

| | |
|---|---|
| Spec | FMB-1.3 (batch spec 2026-08-17); mechanism: overnight equity premium, pre-RTH slice |
| Data | NQ 1m extended fixtures (00:00–16:31 ET) 2020-01-02 → 2026-08-12, 1,704 usable sessions; tick-year close flow for the inventory refinement |
| Geometry | Long 1 NQ at 03:00 ET, fraction-rebased 100/50 bracket, hard backstop 08:30 ET |
| Costs | $10/RT + 1-tick adverse entry + 1-tick adverse stop fill; adverse-first bar resolution |
| Machinery | `scripts/fmb1_studies.py odc` → `var/fmb1/odc.json` (inventory via `var/fmb1/paid_close_flow.json`) |
| Run date | 2026-08-17 |

---

## 1. The window return (02:30 → 08:30 ET, 1,704 sessions)

- **All sessions**: mean +1.5 bp, median +3.4 bp, 54.0% positive,
  t = 1.04. The drift leans long but is *far* from significance at
  session grain.
- **By year** (mean bp): 2020 +4.9 · 2021 +0.4 · 2022 −4.7 · 2023 +0.7
  · 2024 +2.9 · 2025 +2.2 · 2026 +5.5. Recent-4-years-positive: 3 of 4
  (2022 is the miss) — this gate leg passed; nothing else did.
- **By day-of-week** (mean bp): Mon +5.3 (t 1.57) · Tue +1.8 · Wed +3.7
  · Thu −2.2 · Fri −1.4. Monday-effect-shaped, but no cell significant.

## 2. Magnitude — the geometry question answered first

Median window high–low range: **0.614%** of price, vs the 0.331%
target fraction and the 0.198% sixty-point-equivalent floor. The window
moves enough to clear the bracket; a kill here cannot be blamed on the
target being out of range. (Item 3 of the Phase-1 design is therefore
answered: geometry feasible, no re-geometry claim available.)

## 3. First-passage brackets — kill on both legs, inversion in the conditioning

Long at 03:00 ET, backstop 08:30:

| cell | n | hit | stop | time | EV/trade | t |
|---|---|---|---|---|---|---|
| unconditional | 1,704 | 25.4% | 58.6% | 16.0% | −$7 | −0.41 |
| align = 0 | 378 | 28.6% | 60.9% | 10.6% | −$24 | −0.62 |
| align = 1 | 818 | 26.8% | 57.2% | 16.0% | +$29 | 1.07 |
| align = 2 (conditioned) | 508 | **20.9%** | 59.1% | 20.1% | −$53 | −1.62 |

Both the unconditional (25.4%) and conditioned (20.9%) rates sit below
the 36% kill line. Worse: the alignment conditioning (prior-close→00:00
"Asia" sign + 00:00→03:00 "Europe" sign, both up = 2) was pre-registered
to *raise* the hit rate monotonically. It is monotone — **downward**.
Recorded as an inversion/anomaly per FRB-1 rule: full alignment going
into 03:00 marks short-term exhaustion at this horizon, not
continuation. Not promoted; revisiting the reversed sign is a new spec.

## 4. Inventory refinement (tick year only, n = 257)

Prior-session close imbalance (15:50–16:00 signed aggressor flow) as
the dealer-inventory proxy — the one conditioning that pointed the
predicted way:

| cell | n | hit | EV/trade |
|---|---|---|---|
| prior net SELL into close | 147 | 27.9% | +$44 |
| prior net BUY into close | 110 | 20.0% | −$64 |
| net sell AND align = 2 | 49 | 22.5% | −$61 |

Direction as the intermediary-compensation channel predicts, magnitude
nowhere near the bar, and stacking it on the (inverted) alignment
condition helps nothing. One tick year; descriptive only.

## 5. What is recorded

1. **The drift exists but the bracket is the wrong harvester.** Time
   exits (position alive at 08:30) averaged **+$305/trade** across all
   cells — the window drifts up when it doesn't hit a barrier — while
   the 2:1 bracket's asymmetric first-passage burns it: a 50-pt stop is
   touched long before a 100-pt target in a window whose median total
   range is ~186 rebased points. Per batch rule 5, re-geometry (wider
   stop, hold-to-08:30, no bracket) is a NEW spec, not a Phase-1 tweak
   — and the repo's hold-duration law already covers "hold the window"
   designs' costs of validation.
2. **The alignment inversion** is the batch's cautionary exhibit: a
   literature-motivated conditioning, pre-registered in good faith,
   pointing significantly the other way (t −1.62 on EV) inside a
   four-study batch. It is an anomaly entry, not a candidate.
3. The firm-rule prerequisite (pre-market entries permitted) was never
   reached — ODC died before Phase 2 on data alone, which is the
   overlay-first doctrine doing its job.
4. Sunday-open exclusion, DST handling, and the 03:00 entry bar were
   all mechanical; no discretionary cuts were made beyond the spec.

## 6. Verdict

**Dead as specified.** No strategy program, no cycles, no shadow slot,
$0 spent. The +$305 time-exit average and the inventory-direction
agreement are recorded for any future overnight-hold spec, which would
need its own budget, its own gate, and the firm-rule check first.
