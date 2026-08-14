# GFM — Gamma-Conditioned Flow Momentum: Historical Results

**Program status: ACCRUING. Track A (the historical overlay) points
exactly the way the mechanism predicts — all of fc_t13's develop profit
sat on negative-gamma days and positive-gamma days lost money — but
fails its pre-registered gate on statistical power (26 POS-day trades vs
40 required, p = 0.118 vs 0.10). No GFM strategy module is built, no
protocol budget is spent, and per spec-owner direction (2026-08-14)
Track B forward tagging is NOT yet automated — it runs manually.**

| | |
|---|---|
| Spec | GFM v1.0 (fifth program; reuses the validated fc_t13 Flow Core, separate from TFR's cycle budget) |
| Data | 240 sessions of QQQ chain snapshots, 2025-08-14 → 2026-08-14, from OPRA via the existing Databento key |
| Actual cost | ≈ $43 backfill + $0.18/session manual forward tagging (spec budgeted "low hundreds") |
| Machinery | `nq_agent/gex.py` (pure calculator, 14 tests) · `scripts/snapshot_gamma.py` (snapshot/tag) · `scripts/track_a_gamma.py` (join + gate) · raw chains archived per day under `var/gamma/raw/` |
| Trading impact | None. Annotation infrastructure only; the fc_t13 shadow is untouched |
| Report date | 2026-08-14 |

---

## 1. The historical gamma regime series (what the year looked like)

240 sessions classified pre-open from the prior night's chain
(NetGEX in $B; dealer convention +calls/−puts; ZeroFlip from spot-grid
recomputation):

- **NetGEX range**: min −3.55B · median **−0.67B** · max +1.39B
- **Regime mix: 221 NEG / 1 NEUTRAL / 18 POS** — the naive all-OI
  convention reads QQQ's persistently put-heavy chain as negative dealer
  gamma nearly always (flip typically 3–6% above spot)
- POS days cluster: **7 of 18 in one December 2025 window**
  (12-10 → 12-18); the rest scattered (Jan ×3, Mar ×2, Apr ×3, May ×1,
  Jun ×2); four months had none at all
- Monthly: every month ≥ 10 NEG days; December 2025 is the only month
  with a substantial POS presence

This saturation is the defining feature of the year and the reason the
gate is underpowered — not a data defect (every number reproduces from
archived chains; the inverse-convention recomputation mirrors exactly).

## 2. Track A: the trade-level split (307 fc_t13 develop trades)

| bucket | n | net $ (NQ) | EV/trade | win% |
|---|---|---|---|---|
| NEG days | 266 | **+$94,952** | **+$356.96** | 48% |
| POS days | 26 | **−$413** | **−$15.90** | 42% |
| untagged | 15 | −$310 | −$20.65 | 47% |
| pooled | 307 | +$94,228 | +$306.93 | 48% |

- NEG−POS difference **+$372.86/trade**, bootstrap p = 0.1178 (10,000
  resamples, one-sided in the mechanism's direction)
- **Every dollar of develop profit came from NEG days**; the direction
  matches Baltussen et al.'s hedging-demand mechanism exactly
- Pre-registered gate: **FAIL** on n (26 POS < 40) and p (0.118 > 0.10);
  the NEG ≥ 1.5×-pooled criterion was found arithmetically unsatisfiable
  under bucket saturation (recorded spec defect, not patched post hoc)

## 3. What weakens the thesis (recorded with equal weight)

1. **No within-regime dose-response.** Walk-forward tercile ranks of
   NetGEX (the declared sweep) show no gradient: more-negative days are
   not better (33/66 boundaries: NEG-tercile EV $150 vs NEUTRAL $441 vs
   POS-tercile $308; p ≈ 1.0). If the effect is real, it lives at the
   absolute sign/flip boundary — resting on those 26 POS trades.
2. **The gate is not streak protection.** The three worst losing streaks
   (7 days May 27–Jun 4 at −$958/micro; 6 days Jun 12–24 at
   −$1,215/micro; 5 days Mar 6–12) were **20/23 NEG-regime days** — the
   drawdowns live inside the regime the gate keeps. GFM's surviving
   claim is an EV filter, not drawdown protection, and any future sizing
   must assume the full drawdown profile survives the gate.

## 4. Ladder position and what happens next

Per the pre-registered ladder (spec §5): Track A = insufficient to pass,
no reverse split so no kill → **extend with forward accrual and re-read
when the POS bucket reaches 40 trades**, declared before reading.

Current operating decisions (spec owner, 2026-08-14):

- **Track B tagging is manual, not scheduled.** To tag a day (idempotent,
  ~$0.18, any time after ~06:35 ET):
  `uv run python scripts/snapshot_gamma.py`
  Days without a snapshot are simply untagged; classification never
  guesses. At the historical base rate (18 POS days/year, ~1.4 trades/
  day), the POS bucket needs roughly **another year of tagged trading**
  to reach n = 40 — December-style positive-gamma windows are where the
  evidence accrues fastest.
- No GFM strategy module until a track passes; the ~$43 already spent is
  the cost of *not* building on an unproven filter.
- The pipeline (calculator, archive, tagger, join) is permanent
  infrastructure regardless of GFM's fate.

## 5. Reproduce

```
# historical series (already on disk; re-runs are incremental)
uv run python scripts/snapshot_gamma.py --backfill 2025-08-14 2026-08-13

# the Track A join + gate evaluation
uv run python scripts/track_a_gamma.py --regimes var/gamma/regimes.jsonl \
    --journals var/tfr/fc_t13/journal var/shadow/journal
```

Full gate arithmetic, saturation analysis, tercile tables and the streak
cross-reference: `GFM-TRACK-A.md`. Every regime decision reproduces from
the archived chain snapshots.
