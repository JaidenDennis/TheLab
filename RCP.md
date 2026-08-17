# RCP — Roll-Period Calendar Pressure: Phase-1 Report (FMB-1.1)

**Status: KILLED at Phase 1 (2026-08-17), $0 spent. One offset day of
eight shows ≥ 70% sign consistency (three were required), and the
walk-forward bracket hit 16.7% against a 42% bar (kill line 36%). The
spec's volume-migration assumption is also false on disk: the
continuous-contract fixtures carry no per-contract volumes, so the
liquidity-migration curve is not measurable at $0. Do not re-litigate
from memory; re-run `uv run python scripts/fmb1_studies.py rcp`.**

| | |
|---|---|
| Spec | FMB-1.1 (batch spec 2026-08-17); mechanism: scheduled quarterly roll flow |
| Data | NQ 1m pseudo-tick fixtures 2020-01-02 → 2026-08-12 (on disk, $0); 26 quarterly expiries |
| Geometry | 1 NQ, one shot 10:00 ET, fraction-rebased 100/50 bracket (0.3306%/0.1653% of price at spec-lock NQ 30250), 15:55 flatten |
| Costs | $10/RT + 1-tick adverse entry + 1-tick adverse stop fill; adverse-first bar resolution |
| Machinery | `scripts/fmb1_studies.py rcp` → `var/fmb1/rcp.json` |
| Run date | 2026-08-17 |

---

## 1. The roll-day return signature (26 rolls, RTH open→close)

Offsets are trading days before the third-Friday expiry (deterministic
CME calendar). Returns are session fractions; sign consistency is the
majority share across 26 rolls.

| offset | mean | median | consistency | majority | t |
|---|---|---|---|---|---|
| T−8 | +0.08% | +0.14% | 61.5% | long | 0.38 |
| T−7 | −0.24% | −0.33% | 57.7% | short | −1.11 |
| T−6 | −0.23% | +0.20% | 57.7% | long | −0.80 |
| T−5 | −0.19% | +0.09% | 53.8% | long | −0.82 |
| **T−4** | **+0.18%** | **+0.54%** | **73.1%** | **long** | 0.63 |
| T−3 | −0.11% | −0.13% | 57.7% | short | −0.46 |
| T−2 | +0.12% | +0.17% | 50.0% | long | 0.42 |
| T−1 | +0.04% | +0.15% | 57.7% | long | 0.15 |

One offset (T−4, 73.1% long) clears the 70% consistency bar. The
pre-registered proceed-gate required **three**. No offset's mean return
carries a t-stat above 1.2 in magnitude — the "signature" is noise with
one possibly-lucky cell.

## 2. Walk-forward bracket simulation

Direction per offset set from prior rolls only (≥ 12 prior rolls,
prior consistency ≥ 70% to trade). Only offsets T−2 and T−4 ever
qualified, producing 12 trades in six years:

| cell | n | hit | stop | time | EV/trade |
|---|---|---|---|---|---|
| all | 12 | 16.7% | 58.3% | 25.0% | −$72 |
| T−2 | 4 | 0% | 75% | 25% | −$467 |
| T−4 | 8 | 25% | 50% | 25% | +$126 |

Hit rate 16.7% is below the 36% kill line, far below the 42% bar and
the 34% break-even. n = 12 versus the gate's minimum of 50 — six years
of history cannot produce enough qualifying roll days for this design
to ever reach statistical power at this cadence.

## 3. What is recorded

1. **Killed by pre-registered condition**: fewer than 3 stable offsets
   AND hit rate < 36% on what traded. Both kill legs fired.
2. **Volume-migration curve: not measurable at $0.** The spec asserted
   per-contract volumes exist "in the continuous-contract build"; the
   fixtures are the volume-rolled outright only. A per-contract pull
   would be a new (small) data decision, but with the return signature
   this weak there is nothing for the curve to rescue.
3. **The structural story survives; the tradeable never appeared.** The
   roll is real and scheduled, but whatever pressure it exerts on the
   outright is too small and too inconsistent at daily resolution to
   carry a 2:1 first-passage bracket.
4. Recorded limit from the spec confirmed in the extreme: ~8–12
   candidate days per quarter shrank to ~2 walk-forward-qualified
   trades per year. Even a positive result would have been a sleeve;
   this is not a positive result.

## 4. Verdict

**Dead.** No strategy program, no cycles, no shadow slot. Any revival
(e.g., calendar-spread expression instead of the outright, or
tick-level roll-day microstructure) is a new spec with its own budget.
