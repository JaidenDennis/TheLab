# CST — Cross-Asset Shock Transmission: Phase-1 Report (FMB-1.2)

**Status: KILLED at Phase 1 (2026-08-17), $0 spent (the ZN pull quoted
$0.00 inside the plan — 123.5 MB). Transmission from ZN shocks into NQ
at minute cadence is statistically absent: the best horizon (5m) shows
+1 bp with t = 1.85 and a median of zero, decaying to nothing by 30m;
0 of the required 2 horizons reach T ≥ 2.0; the bracket hit 27.7%
against a 36% kill line. What little signal exists completes inside the
first minutes — the spec's own "lag too fast for the cadence" kill
clause fires. Re-run: `uv run python scripts/fmb1_studies.py cst`.**

| | |
|---|---|
| Spec | FMB-1.2 (batch spec 2026-08-17); mechanism: rates-shock transmission, ZN → NQ |
| Data | ZN.v.0 1m RTH fixtures 2020-01-02 → 2026-08-14 (new pull, $0.00 quoted, `var/fixtures/zn-1m/`) + NQ 1m fixtures on disk |
| Shock | ZN 5m return beyond trailing-60-session per-minute sigma × K (K = 2.5 declared; 2.0/3.0 counted); first shock per session; FOMC 13:55–14:30 excluded on FOMC days; 10:00–10:02 flagged scheduled |
| Geometry | 1 NQ at next 1m open in the transmitted direction (notes down → NQ short), fraction-rebased 100/50 bracket, 90-min backstop, 15:55 flatten |
| Costs | $10/RT + 1-tick adverse entry + 1-tick adverse stop fill; adverse-first bar resolution |
| Machinery | `scripts/fmb1_studies.py cst` → `var/fmb1/cst.json` |
| Run date | 2026-08-17 |

---

## 1. The shock catalog — the premise is weaker than the spec assumed

- Shock **minutes** across 1,712 sessions: 35,205 at 2.0σ · 17,738 at
  2.5σ · 9,656 at 3.0σ. A 2.5σ per-minute envelope on ZN 5m returns is
  not a rare-event detector — it fires somewhere in the session on
  **1,376 of 1,712 days** (80%). "First qualifying shock per session"
  therefore conditions on almost nothing; the study is closer to "NQ
  after ZN's biggest early move" than "NQ after a discrete rates
  event". This is recorded as a premise defect, measured honestly by
  the declared K rather than tuned away.
- 1,322 of 1,376 shock days are unscheduled (n floor of 100 easily
  met — power is not the problem here; the effect is).

## 2. Transmitted-direction forward returns (unscheduled, n = 1,317)

| horizon | mean | median | share > 0 | t |
|---|---|---|---|---|
| 5m | +0.97 bp | 0.0 | 49.7% | 1.85 |
| 15m | +1.1 bp | 0.0 | 49.6% | 1.29 |
| 30m | +0.8 bp | 0.0 | 49.9% | 0.67 |
| 60m | −0.4 bp | −0.6 bp | 49.2% | −0.30 |
| 90m | +0.6 bp | +0.4 bp | 50.3% | 0.35 |

**Gate: FAILED** (0 of 2 horizons at T ≥ 2.0). The profile is the
classic already-priced shape: a ~1 bp residue at 5m that a median of
zero shows is carried by a few tails, gone by the hour. This matches
the FRB-1 LL finding one asset over: at minute resolution, cross-market
information is in the price before a 1m-cadence follower can act.

## 3. The bracket panel (for completeness — the gate above already killed it)

| cell | n | hit | stop | time | EV/trade | t |
|---|---|---|---|---|---|---|
| all shocks | 1,371 | 27.7% | 59.8% | 12.5% | −$6 | −0.29 |
| unscheduled only | 1,317 | 27.3% | 59.8% | 12.9% | −$10 | −0.45 |

Below break-even (34%), far below the bar (42%), six years, no cell to
argue about.

## 4. What is recorded

1. **Killed by pre-registered condition** — transmission absent at
   every gated horizon, and the residual 5m concentration triggers the
   spec's "lag too fast for the cadence" clause. A tick-level CST would
   be a new program with its own budget; nothing measured here funds
   optimism about it.
2. **The shock definition needs rethinking before any revival**: an
   envelope that fires on 80% of sessions is measuring ZN's daily
   range, not discrete repricing. A future spec should gate on
   magnitude percentiles of the *daily first shock* or on event ticks,
   not per-minute sigma alone.
3. First cross-asset driver ever ingested by the repo: the ZN fixture
   set (1,712 sessions, $0) and the per-minute envelope machinery now
   exist on disk for any future rates-conditioned study.

## 5. Verdict

**Dead.** No strategy program, no cycles, no shadow slot. The ZN
fixtures remain as reusable infrastructure; the mechanism as specified
does not survive contact with its own catalog.
