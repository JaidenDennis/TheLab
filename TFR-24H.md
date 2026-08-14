# TFR-24H — The Blackout Experiment (Asia + London + RTH)

**Status: EXPLORATORY, complete. Verdict up front: trading all hours with
a 16:30–18:00 ET blackout did NOT beat the declared RTH-only variant —
$58.5k best-cell net vs fc_t13's $94.2k on the same year. Asia is
statistical dead weight; London is a genuine but unproven lead that
prefers the opposite exit style; and the experiment exposed a mechanical
flaw (one shared daily cap starves the best session). Nothing here
touches the running shadow. If any 24h variant is ever declared, it
spends research cycle 2 — this report deliberately does not.**

| | |
|---|---|
| Ask | User direction 2026-08-14: blackout 16:30–18:00 ET only; Asia and London sessions free to trade |
| Data | Full-day tick year 2025-08-14 → 2026-08-12, 260 sessions, **$0** (free trailing L1 window; 7.6 GB); full-day 1m bars ($0, L0) |
| Data QA | 0 sessions excluded; median tick-rule agreement 84.8% |
| Pipeline | Full-day (1..1440) indexing; **per-block calibration** (Asia / London / RTH / close each carry their own trailing-60-session \|F1_5\| percentiles and volume z-stats — a single 24h distribution would let only RTH ever arm) |
| Session mechanics | Engine sessions 00:00–23:55 ET; entries blocked 15:20–18:05 and 22:50–00:05 (buffers sized so a 13-bar hold cannot cross the blackout or midnight); pre-blackout flatten 16:25 (non-terminal — evenings re-arm); midnight flatten 23:45; FOMC block after 13:00 |
| Entries | Flow Core, unchanged: \|F1_5\| ≥ block-Q70, vol_z ≥ 0.5 (block-relative), cap 3/day **shared across all blocks** (see §4) |
| Costs | $10/RT + 1-tick adverse, 1 NQ contract flat |
| Run date | 2026-08-14 |

---

## 1. The five pre-declared cells

| variant | exit | trades | net $ | EV/trade | win% | PF | maxDD pts |
|---|---|---|---|---|---|---|---|
| t24_t13 | 13-bar clock | 695 | 13,827 | +19.89 | 43.7% | 1.06 | 1,779 |
| t24_hf | hostile flow | 707 | 57,856 | +81.83 | 38.0% | 1.22 | 904 |
| t24_fd | flow decay | 706 | 46,597 | +66.00 | 41.9% | 1.25 | 917 |
| **t24_fstack** | decay + hostile | **714** | **58,485** | **+81.91** | 39.8% | **1.33** | **703** |
| t24_t13_q80 | clock, Q80 entries | 636 | −15,283 | −24.03 | 45.1% | 0.97 | 1,258 |

**The exit lesson inverts outside RTH.** In the RTH-only matrix the 13-bar
clock beat every reactive exit ($307 vs $188–225); across 24h the clock
collapses to +$20 and the reactive stacks win by 4×. Mechanism: RTH flow
bursts persist (hold and let them run); overnight bursts are short-lived
in thin tape, and a 65-minute blind hold gives the move back. Exit design
is session-dependent — the "hold duration carries the value" law measured
three times in this program is, more precisely, an RTH law.

## 2. Where the money is, by session block (entry time, ET)

| block | t24_fstack | | | t24_t13 | | |
|---|---|---|---|---|---|---|
| | n / net | EV | T | n / net | EV | T |
| Asia (18:00–03:00) | 290 / $3,585 | **+$12** | **0.33** | 262 / $4,658 | +$18 | 0.33 |
| London (03:00–09:30) | 300 / $30,773 | **+$103** | **1.35** | 295 / $14,126 | +$48 | 0.51 |
| RTH (09:35–15:20) | 124 / $24,127 | +$195 | 1.16 | 138 / −$4,957 | −$36 | −0.21 |

- **Asia is noise at cost**: ~40% of all trades, EV indistinguishable from
  zero (T = 0.33) under both exit styles. The flow signal does not carry
  direction information in the overnight tape at these thresholds.
- **London is the finding**: +$103/trade over 300 trades under reactive
  exits — sub-significant (T = 1.35) but consistent in sign across both
  exit families, and clearly preferring the reactive style.
- **Quarterly (fstack)**: −$6.0k / +$1.3k / +$24.1k / +$21.8k / +$17.3k —
  the first two quarters flat-to-negative, 2026 strong. Not stable enough
  to declare anything.

## 3. Against the declared RTH variant, same year

| | fc_t13 (declared, RTH pipeline) | t24_fstack (best 24h cell) |
|---|---|---|
| Net | **+$94,228** | +$58,485 |
| EV/trade | **+$306.93** | +$81.91 |
| T | **2.58** | ~1.4 |
| Trades | 307 | 714 |
| maxDD | 1,047 pts | 703 pts |

More than twice the trades for 62% of the money at a third of the
per-trade edge. **The blackout schedule as specified is not an upgrade —
it is a dilution.** The only line the 24h experiment adds that the RTH
variant lacks is the London block, and the shared-cap artifact below
means even the RTH rows of this table understate what a clean design
would do.

## 4. The design flaw the experiment exposed (and why RTH looks broken here)

The 3-entries/day cap is shared across the whole 24h session, and Asia +
London fire first — by the time RTH opens, the cap is usually spent.
RTH trade count collapsed from 307 (own pipeline) to ~130 here, and the
survivors are adversely selected (whatever happened to fire before the
cap, not the best RTH signals). That is why t24_t13's RTH row is negative
while the identical logic earns +$307 in its own pipeline. **Any serious
multi-session variant needs per-block caps** — recorded as cycle-2 design
material, not implemented (it would be tuning after seeing results).

## 5. Verdict and recommendations

1. **Do not adopt the 24h schedule.** Asia adds cost-line noise;
   the schedule as a whole earns less than the declared RTH variant while
   trading twice as often.
2. **The shadow stays exactly as declared** (RTH fc_t13). Untouched by
   any of this.
3. **One lead worth banking, not chasing**: London + reactive exits
   (+$103/trade, n=300, T=1.35, sign-consistent). If cycle 2 is ever
   spent, "London block with per-block caps and the fstack exit" is the
   best-evidenced candidate this program has — but spending the last
   cycle is a spec-owner decision, and this report's job is only to
   record the evidence.
4. Methodology note for any future 24h work: per-block calibration
   worked (every block armed and traded); the session-mode mechanics
   (blackout buffers, non-terminal pre-blackout flatten, midnight
   boundary) behaved exactly as designed and are tested/mutation-checked
   in the repo.

## 6. Reproduce

```
uv run python scripts/fetch_fixtures.py --start 2025-08-14 --end 2026-08-12 \
    --schema trades --window full --chunk-days 1 --out var/fixtures/trades-full
uv run python scripts/fetch_fixtures.py --start 2025-08-14 --end 2026-08-12 \
    --schema ohlcv-1m --window full --out var/fixtures/1m-full
uv run python scripts/precompute_flow.py --ticks var/fixtures/trades-full --out var/flow24 --full
uv run python scripts/build_decisions24.py --flow var/flow24 --out var/decisions24
uv run python scripts/run_tfr24.py --fixtures var/fixtures/1m-full \
    --decisions var/decisions24 --out var/tfr24 --variant all
```

Cells: `scripts/run_tfr24.py::build_variants`. Same engine, same costs,
same journal — nothing here runs outside the production stack.
