# SME — Session-Momentum Expansion: Develop-Window Results

**Status: one configuration passes the pre-registered develop gate. The
holdout has NOT been touched. Nothing here is validated for live use.**

| | |
|---|---|
| Strategy | SME v1 (`src/nq_agent/strategy/sme.py`), spec v1.0 |
| Data | NQ.v.0 (volume-rolled continuous), Databento GLBX.MDP3, 1m OHLCV pseudo-tick fixtures, extended window (00:00–16:31 ET) |
| Develop window | 2021-01-04 → 2024-09-30, 969 sessions |
| **Holdout (sealed)** | **2024-10-01 → 2026-08-12 — untouched, to be read exactly once** |
| Sizing | 1 NQ contract flat ($20/pt). All dollar figures are per-contract; divide by 10 for MNQ |
| Costs | $10/contract round turn **plus** a modeled 1-tick adverse fill on every stop-market entry (≈$5 of slippage double-counted — the model is deliberately conservative) |
| Engine | The production engine (same fill model, risk layer, journal); backtests replay through it, no separate simulator |
| Run date | 2026-08-13 |

---

## 1. Verdict

- **v1 as specified loses money**: −$115/trade with all four years negative.
  The break-even + 5m-trail exit stack is the single largest destroyer of
  value in the design.
- **Every layer independently adds EV** (the spec's §8.5 requirement): the
  context filter is worth ≈ +$23/trade, the OFI gate ≈ +$93/trade with v1
  exits and ≈ +$142/trade with hold exits.
- **One cell of the exit×gate matrix is positive: A+B+C with hold-to-close
  exits — +$86.27/trade, PF 1.19, positive in all four years, and
  *improving* over time** (the opposite of the crowding-decay signature that
  killed the ungated variants).
- That cell **passes the pre-registered develop gate** (EV ≥ $40/trade, ≥3
  of 4 years positive). Per protocol, the next and only next step is a
  single read of the sealed holdout. **Expect degradation there**: this cell
  was selected as the best of five examined variants, and selection inflates
  develop-set results even when each variant was spec-motivated.
- Economics if (and only if) the holdout holds: ≈12.8 trades/month at
  +$86/trade ≈ **$1,100/month per NQ contract ≈ $110/month per MNQ micro**,
  with a closed-trade max drawdown of ≈$2,080 per micro against a typical
  $4,500-class trailing limit. Viable at 1–2 micros; thin but real.

---

## 2. The ablation matrix

Five variants, same engine, same fixtures, same costs, isolated state.

| variant | exits | OFI gate | trades | net $ | EV/trade | win% | PF | maxDD (pts) |
|---|---|---|---|---|---|---|---|---|
| B alone | v1 (BE+trail) | off | 1,424 | −196,451 | −137.96 | 38.6% | 0.69 | 9,344 |
| A+B | v1 | off | 648 | −74,795 | −115.42 | 40.3% | 0.74 | 3,666 |
| A+B | hold | off | 706 | −39,094 | −55.37 | 23.2% | 0.91 | 3,143 |
| A+B+C | v1 | proxy | 528 | −11,642 | −22.05 | 47.9% | 0.97 | 886 |
| **A+B+C** | **hold** | **proxy** | **578** | **+49,861** | **+86.27** | **27.9%** | **1.19** | **1,041** |

"hold" = initial stop + 15:55 flatten only (no break-even move, no trail).
"proxy" = per-bar volume-delta OFI (spec §5.2 fallback); true aggressor-side
OFI was not available for the develop window (see §7).

**Layer contributions** (EV deltas): Context ≈ +$23. Gate ≈ +$93 under v1
exits, ≈ +$142 under hold. Hold exits ≈ +$60 ungated, ≈ +$108 gated. The
gate and the hold exit are synergistic: the gate selects entries whose tails
are worth holding for; the trail had been capping those same tails, which
made the gate's selectivity almost worthless under v1 exits.

---

## 3. The winning cell in detail (A+B+C, hold exits)

### By year — positive all four, trending up

| year | trades | net $ | EV/trade | win% |
|---|---|---|---|---|
| 2021 | 131 | +3,539 | +27.01 | 28% |
| 2022 | 150 | +11,006 | +73.37 | 27% |
| 2023 | 177 | +22,123 | +124.99 | 29% |
| 2024 (Jan–Sep) | 120 | +13,193 | +109.94 | 27% |

### Trade geometry — the spec's intended shape, finally realized

| | value |
|---|---|
| Average win | **+$2,161** (n=161) |
| Average loss | −$715 (n=417) |
| Win : loss ratio | 3.0 : 1 |
| Win rate | 27.9% (breakeven ≈ 25%) |
| Exit mix | 170 FLATTEN (avg +$2,026) / 408 STOP (avg −$722) |
| Cadence | 12.8 trades/month (median month: 12) |
| Reached +1R / +2R / +3R before 15:55 | 76% / 56% / 38% |

### Directional and calendar cuts

| cut | n | EV/trade | note |
|---|---|---|---|
| LONG | 314 | **+$137.28** | positive every year ($54 / $65 / $231 / $164) |
| SHORT | 264 | +$25.58 | positive but thin; the gate rescued a side that loses badly ungated |
| 10:00 ET entries | 390 | +$91.16 | the core bucket works **only** in this cell |
| 11:00–12:00 entries | 116 | +$206 avg | strongest window |
| 14:00+ entries | 46 | −$224 avg | late entries are bad in every variant |
| Mon / Wed | 217 | −$117 avg | the two losing weekdays |
| Tue / Thu / Fri | 361 | +$208 avg | carry the strategy |

Frequency context: layer A trades ~47% of sessions; the gate then passes
~80% of armed attempts. 44 of 45 develop months had at least one trade.

---

## 4. Why v1's exits failed: the numbers

Same A+B entries under the two exit models:

| exit model | avg win | avg loss | ratio | win% | needed to break even |
|---|---|---|---|---|---|
| v1 (BE at +1R, 5m trail) | $727 | −$684 | 1.06 : 1 | 40.3% | ~48% |
| hold-to-close | $2,268 | −$675 | 3.36 : 1 | ~23% | ~23% |

The trail did not reduce losses at all (the initial stop was already doing
that job); it only cut the average winner by ~$1,500. 57% of all entries
touched +2R before 15:55 and 38% touched +3R — the tails the thesis
predicted exist, and the v1 exit stack was systematically amputating them.
The break-even move was the worst offender: 75% of entries touch +1R, so
almost every trade armed a stop two ticks past entry and got scratched on
ordinary noise.

---

## 5. Where it loses (all variants)

- **Ungated shorts are toxic**: −$148 to −$176/trade in every ungated
  variant. Index drift is real. Only the OFI gate makes shorts survivable.
- **The 10:00 hour is where ungated variants die** (~80% of trades,
  −$78 to −$136 EV). The first hour's breakouts are the crowded trade;
  without flow confirmation they are noise-chases.
- **Late entries (14:00+) lose everywhere**, including the winning cell.
  A v1.1 could justifiably end the entry window at 13:00 — but that is a
  *new hypothesis for the next develop cycle*, not something to bolt on
  before the holdout.
- **Monday/Wednesday lose in most variants** — noted, unexplained, and
  deliberately NOT filtered out (no mechanism, smells like noise).
- **Ungated equity curves decay monotonically 2021 → 2024** — the
  crowding signature. The gated stack does not show it. That is either the
  gate genuinely restoring selectivity, or 2023–24 regime luck. The holdout
  (which is mostly 2024-10 → 2026-08) will answer this question directly.

---

## 6. Methodology corrections made along the way

Recorded so nobody re-inflates these numbers later:

1. **A replay counterfactual overstated the hold exit by ~$109k.** An early
   per-trade replay ("hold each v1 entry to 15:55") showed +$70k; the
   engine's true hold variant showed −$39k on ungated A+B. The replay priced
   a trade set the strategy cannot take (overlapping holds on two-trade
   days; none of the extra re-arm entries that holding creates). Engine
   numbers are the only ones this report cites for variant P&L. The
   "initial-stop-only net" lines in the raw diagnostics output are that same
   flawed estimator — ignore them.
2. **Two engine bugs were found and fixed during (and because of) this
   work**, both mutation-tested: cross-session resume was carrying
   yesterday's halt/trade-count/daily-loss into today, and session-end
   strategy state (the rolling stats layer A needs) was never persisted —
   the first A+B run took zero trades in 947 sessions because of it.
3. **1m pseudo-tick fixtures** rebuild the engine's bars 99.2% identically
   to true-tick fixtures (8 of 504 bars/day differ by minute-boundary
   trades of 1–2 contracts). Fidelity measured, not assumed.

Standing model caveats: no spread/latency beyond the $10 + 1-tick model; no
partial fills; closed-trade drawdown only (intra-trade is worse and is what
prop trailing limits actually hit); calendar filters (FOMC/news) ran empty —
no dates were supplied, so §3.4 of the spec was effectively OFF in all runs.

---

## 7. Known gaps against the spec

- **OFI is the proxy, not the real thing.** True aggressor-side OFI needs
  tick data, which is on disk only for 2025-08 → 2026-08 (mostly holdout).
  Buying develop-era ticks (~$5.5/month of history) would allow a
  full-vs-proxy comparison; the spec expected full to be stronger.
- Exhaustion filter (§3.2) saw only the 00:00–09:30 overnight portion
  (engine sessions are calendar days). Weakens the filter, never the entries.
- Sizing (§6.1) not simulated — per-contract EV only. Note the spec's $400
  cap cannot size full NQ at these R values (~$700/trade); MNQ is the
  sizing-compatible instrument.
- Prop-firm equity replay (§8.7) not yet run; the closed-trade DD figures
  above are the floor estimate.

---

## 8. Protocol state and the committed next step

Pre-registered gate (set before the winning cell was read):
**EV ≥ +$40/trade and ≥3 of 4 develop years positive.**

- A+B+C-hold: **PASSES** (+$86.27, 4/4 years).
- Selection caveat: best-of-five on the develop window. The honest prior is
  that holdout EV lands well below +$86.

**Committed next step, per protocol: run A+B+C-hold (exact current
parameters, no changes) on the sealed holdout, 2024-10-01 → 2026-08-12,
exactly once.** Survives → shadow mode per RUNBOOK. Fails → the strategy is
dead as specified; layers A and C remain validated components for the next
thesis.

The user has separately judged the return profile (~$110/month per micro)
too thin to pursue; that is a portfolio decision, not a data one. This
report records what the data showed either way. If SME is shelved without
the holdout read, the holdout stays sealed and reusable.

### Candidate next theses (in evidence order)

1. **Failed-breakout fade**: 72% of ungated triggers stop out; the fade
   side of the exact same levels has been collecting what SME paid,
   with the OFI gate available in reverse (fade breakouts on *weak* flow).
2. Layer A + C as filters on a different entry (pullback-continuation
   rather than breakout).

---

## 9. Reproduce

```
# fixtures (free on the CME Standard plan)
uv run python scripts/fetch_fixtures.py --start 2021-01-04 --end 2026-08-12 \
    --schema ohlcv-1m --window extended --out var/fixtures/1m

# the matrix (develop window only)
uv run python scripts/run_ablations.py --fixtures var/fixtures/1m \
    --start 2021-01-04 --end 2024-09-30 --out var/ablations

# per-trade diagnostics for any variant
uv run python scripts/diagnose_sme.py --journal var/ablations/a_b_c_hold/journal \
    --fixtures var/fixtures/1m --out var/ablations/a_b_c_hold-diagnostics.json
```

Variant definitions: `scripts/run_ablations.py::VARIANTS`. Everything runs
through the production engine; there is no separate backtest simulator to
drift out of sync.
