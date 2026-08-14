# GFM Track A — Historical Gamma Overlay: Report

**Status: NOT PASSED, NOT KILLED. The split points exactly the way the
mechanism predicts — all of fc_t13's profit sits on negative-gamma days
and positive-gamma days are net losers — but the pre-registered gate
fails on statistical power: the year was so persistently negative-gamma
that only 26 trades landed in the POS bucket (40 required), and
p = 0.118 misses the 0.10 bar. The spec's own prescription for exactly
this outcome applies: extend with forward accrual (Track B, running at
$0.18/day) and re-read when the POS bucket fills. The GFM strategy
module is correctly NOT built yet.**

| | |
|---|---|
| Data | 240 sessions tagged, 2025-08-14 → 2026-08-14; QQQ chain from OPRA (definitions + 06:30 ET OI batch + prior-close mids); every parsed chain archived under var/gamma/raw |
| Actual cost | ≈ $43 backfill + $0.18/day forward (spec budgeted "low hundreds") |
| Calculator | nq_agent/gex.py — per-contract IV solved from mids, parity-implied spot, ZeroFlip via spot grid; convention and 0DTE filter explicit; 14 tests |
| Trades joined | 307 fc_t13 develop trades (+ shadow accrual as it arrives); 15 trades untagged (sessions outside the window) |
| Join type | Column-join on trades that already happened — no backtest, no protocol budget spent |

## 1. The primary split (spec 2.3 classification)

| bucket | n | net $ | EV/trade | win% |
|---|---|---|---|---|
| NEG days | 266 | **+$94,952** | **+$356.96** | 48% |
| POS days | 26 | **−$413** | **−$15.90** | 42% |
| pooled | 307 | +$94,228 | +$306.93 | 48% |

- NEG−POS EV difference: **+$372.86/trade**, bootstrap p = **0.1178**
  (10,000 resamples, one-sided in the mechanism's direction).
- **Every dollar of the strategy's develop profit came from NEG days.**
- Inverse-convention recomputation mirrors exactly (no alternative
  reading of the same data produces a competing story).

### Why the gate still fails, formally

| criterion | bar | result | verdict |
|---|---|---|---|
| n ≥ 40 in each bucket | 40/40 | 266 / **26** | **FAIL** |
| NEG ≥ 1.5× pooled | ≥ $460 | $357 | FAIL (see note) |
| POS ≤ 0.5× pooled | ≤ $153 | −$16 | PASS |
| bootstrap p | < 0.10 | 0.118 | **FAIL** |
| mechanism direction | NEG > POS | NEG > POS | PASS |

Note on the 1.5× criterion: with 87% of trades in the NEG bucket, pooled
EV ≈ NEG EV by construction, so NEG can never reach 1.5× pooled. The
criterion silently assumed balanced buckets; under saturation its
arithmetic is unsatisfiable rather than informative. Recorded as a spec
defect to fix in any amendment (e.g., NEG ≥ 1.5× **POS-complement** EV),
not patched retroactively.

## 2. Why the year saturated

The all-OI dealer-inventory convention reads QQQ's persistently
put-heavy chain as negative net gamma nearly always: 221 of 240 sessions
classified NEG (ZeroFlip typically 3–6% above spot). This is a known
behavior of the naive convention in index products, and it does not
falsify the mechanism — it means the classifier's absolute-sign branch
rarely yields the contrast the gate needs within a single year.

## 3. The declared tercile sweep (walk-forward relative ranks)

Ranking each day against its trailing-120-session NetGEX distribution
(de-saturating by construction):

| boundaries | NEG n / EV | NEUTRAL n / EV | POS n / EV | NEG−POS p |
|---|---|---|---|---|
| 40/60 | 97 / $195 | 66 / $483 | 129 / $339 | 1.00 |
| 33/66 | 70 / $150 | 118 / $441 | 104 / $308 | 1.00 |
| 25/75 | 45 / $336 | 167 / $346 | 80 / $271 | 0.43 |

**No within-regime dose-response**: among days that are almost all
negative-gamma in absolute terms, *more* negative does not mean *more*
edge — if anything the middle of the distribution was best. The effect,
if real, lives at the absolute sign/flip boundary (the 26-trade POS
bucket), not on a relative gradient. This sharpens what Track B must
test and is honestly the weakest result in this report for the thesis.

## 4. Verdict and the path forward

Per the pre-registered ladder (spec 5.1/5.3):

1. **Track A: insufficient evidence to pass; no reverse split, so no
   kill.** The n < 40 branch prescribes extension with forward accrual.
2. **Track B continues automatically** — the daily pre-open snapshot
   ($0.18) tags every shadow session (today: NEG, NetGEX −$824M). POS
   days accrue at whatever rate the market supplies them; the re-read
   happens when the POS bucket reaches 40 trades, declared before
   reading, alongside the shadow-completion read.
3. **No GFM strategy module is built** until a track passes — the spec's
   build order held, and ~$43 bought the answer that prevents building
   on an unproven filter.
4. The gamma pipeline (calculator, archive, daily tagging) is permanent
   annotation infrastructure regardless of GFM's fate.

## 5. Reproduce

```
uv run python scripts/snapshot_gamma.py --backfill 2025-08-14 2026-08-13
uv run python scripts/track_a_gamma.py --regimes var/gamma/regimes.jsonl \
    --journals var/tfr/fc_t13/journal var/shadow/journal
```

Every regime decision is reproducible from the archived chains in
var/gamma/raw; the tercile sweep is scripts/track_a_gamma.py's declared
companion analysis (walk-forward percentile ranks).

## 6. Post-report check: the losing streaks are NEG-regime events (2026-08-14)

Prompted by a drawdown question: the three worst losing streaks in the
fc_t13 record were cross-referenced against the regime series. 20 of 23
streak days were NEG — the baseline mix (91% NEG among traded days). The
record 7-day streak (2026-05-27 → 06-04, −$958/micro) was NEG all seven
days; the costliest (2026-06-12 → 06-24, −$1,215/micro) was NEG on five
of six, its lone POS day a knife-edge +$0.04B with spot on the flip.

**Consequence for the thesis:** the EV split (§1) stands, but spec §1.3's
claim that the gate "cuts drawdown by deleting days where dealers are
structurally positioned against the strategy" is directly contradicted —
the major drawdowns are NEG-regime phenomena the gate keeps. If GFM ever
passes its gates, it is as an EV filter, not streak protection, and any
sizing math must assume the full drawdown profile survives the gate.
