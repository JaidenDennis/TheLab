# FMB-1 — Fresh Mechanisms Batch 1: Four Studies, One Scoreboard

**Scoreboard: three kills (RCP, CST, ODC — the last with a recorded
inversion), one promising-but-not-passed (PAID). Zero strategy modules
built, zero protocol budget spent, total data cost $0.00 (the ZN pull
quoted $0.00 inside the plan). The recorded prior — "at most one or two
of the four survive Phase 1" — landed on its pessimistic edge: PAID is
the only mechanism whose evidence points the predicted way, and it
fails its gate on power (n = 47 vs 50; best t 1.64 vs 2.0), not on
direction. Per batch rule 3 all four outcomes are reported together so
the denominator is visible: four studies, ~20 gated cells, one
above-bar bracket cell and one significant inversion — the base rate
this rule exists to display.**

| | |
|---|---|
| Spec | FMB-1 batch (issued 2026-08-17): window phenomena with defined drivers, spec-owner geometry held fixed |
| Geometry (all) | 1 NQ, one shot/session, fraction-rebased 100/50 bracket (0.3306%/0.1653% at spec-lock NQ 30250), hit-rate bar 42%, kill line 36%, break-even ≈ 34% |
| Costs (all) | $10/RT + 1-tick adverse entry + 1-tick adverse stop-market fill; adverse-first bar resolution |
| Data | NQ 1m fixtures 2020→present, NQ tick year, new ZN 1m fixtures 2020→present ($0.00) |
| Machinery | `scripts/fmb1_studies.py` (rcp / cst / odc / paid / overlap) → `var/fmb1/*.json` |
| Reports | `RCP.md` · `CST.md` · `ODC.md` · `PAID.md` (one per program, per spec direction) |
| Run date | 2026-08-17 |

---

## The four verdicts in one table

| program | mechanism | verdict | gate result | headline number |
|---|---|---|---|---|
| FMB-1.1 RCP | quarterly roll pressure | **KILL** | 1 of 3 stable offsets; WF hit 16.7% (n=12) | no offset t beyond ±1.2 |
| FMB-1.2 CST | ZN→NQ shock transmission | **KILL** | 0 of 2 horizons T ≥ 2; hit 27.7% (n=1,371) | 5m residue +1 bp, median 0 |
| FMB-1.3 ODC | overnight drift, pre-RTH slice | **KILL + inversion** | hit 25.4% uncond / 20.9% conditioned | alignment conditioning monotone the WRONG way |
| FMB-1.4 PAID | close-auction inventory unwind | **NOT PASSED — promising** | 0 of 2 horizons T ≥ 2 (best 1.64); n 47 < 50 | bracket 42.6% hit, +$251/trade EV |

## Batch-level findings

1. **The geometry is the common executioner.** Three mechanisms show a
   real underlying drift (ODC time-exits +$305/trade; CST time-exits
   +$266; PAID 120m drift +16 bp) that the 100/50 first-passage bracket
   converts into sub-break-even hit rates. A 50-pt stop is touched
   before a 100-pt target in windows whose drivers are worth 1–16 bp.
   Only PAID — where the driver (a Q80 inventory event) is large
   relative to the morning window — clears the bar. Per batch rule 5
   this is recorded, not "fixed": re-geometry is a new spec per
   program.
2. **Two of the spec's data assumptions failed on contact** and are
   recorded rather than papered over: per-contract roll volumes do not
   exist in the continuous build (RCP's migration curve unmeasurable at
   $0), and a 2.5σ per-minute envelope on ZN fires on 80% of sessions
   (CST's "discrete shock" premise is structurally weak as
   operationalized).
3. **The inversion count is now two across two batches** (FRB-1: SSF
   and LL; FMB-1: ODC alignment). Literature-motivated conditionings
   pre-registered in good faith keep pointing backward at this repo's
   horizons — the strongest standing argument for the
   declare-then-read discipline.

## Independence audit (batch rule 2 — priced, not assumed)

Trade-day sets from each study's own qualifying/simulated days:

| pair | overlap | note |
|---|---|---|
| cst ∩ odc | 409 of 508 ODC days | CST "shocks" on 80% of sessions make this vacuous |
| cst ∩ paid | 43 of 47 PAID days | same cause |
| odc ∩ paid | 21 of 47 | real but moot (both non-survivors at this geometry) |
| rcp ∩ paid | 0 | — |

With three programs dead the portfolio-arbiter question (batch rule 4)
does not arise; recorded for the file only. The CST rows are a further
exhibit of finding 2: a conditioner that fires daily is not a program,
it is an almanac.

## Spend ledger (batch rule 7)

| item | quoted | spent |
|---|---|---|
| RCP / ODC / PAID studies | $0 | $0 |
| ZN 1m 2020→2026 pull (123.5 MB) | $0.00 | $0.00 |
| PAID confirmation tick purchase | ~$250–300 | **not spent** — gated on a pass that did not occur |

## What survives the batch

- **PAID's $0 re-run date**: the free tick window accrues ~4 qualifying
  days/month; n ≥ 50 arrives in ~1 month, estimated power (~80 days)
  in ~8–9 months. The study re-runs byte-identically from
  `scripts/fmb1_studies.py paid`. No purchase, no program, no cycles
  until the effect survives that n.
- **ZN fixture infrastructure** (1,712 sessions) and the per-minute
  envelope applied to a second asset class — reusable by any future
  rates-conditioned spec.
- **Anomaly ledger entries**: ODC's alignment inversion; ODC's
  +$305/trade time-exit drift as raw material for a future
  overnight-hold spec (new budget, firm-rule check first).
