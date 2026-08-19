# TFR shadow declaration — BINDING (signed off 2026-08-13)

Per TFR spec v1.0 §9.3 and the cycle-1 amendment. Spec owner signed off in
session; this commit is the declaration. No parameter may change between
this commit and the end of the shadow window.

**Declared variant: `fc_t13`** — exactly as reported in TFR.md §1:

| parameter | value |
|---|---|
| Entry | completed 5m bar, 09:35–15:00 ET, \|F1_5\| ≥ trailing-60-session Q70, vol_z ≥ 0.5 |
| Direction | sign(F1_5) |
| Regime layer | annotations only (never a gate — invariant-tested) |
| Exit | 13 completed 5m bars (65 min) time exit |
| Backstops | catastrophic stop 0.35% of entry price (server-side, atomic, never tightened); hard flatten 15:55 |
| Caps | 3 entries/day; stop-and-reverse counts 2; FOMC: no entries after 13:00 |
| Sizing (shadow) | 1 MNQ equivalent, constant |
| Costs budget | $10/RT NQ-equivalent + 1 tick adverse; drift audit must show live fills within 1 tick of model on median |

**Shadow gate (pre-registered, unchanged):** live-paper through the
production engine on real-time data for **3 months or 60 trades,
whichever is LONGER**. Pass = net EV ≥ +$20/trade (NQ-equiv) AND PF ≥ 1.10
AND execution-drift audit passes. Pass → smallest funded tier. Fail →
dead; shadow data is spent evidence, no parameter rescue against it.

Shadow clock starts at the first live-paper session, not at this commit.

---

## Addendum — funded-stage sizing policy (recorded 2026-08-14)

**Does not alter the shadow**: the shadow trades a constant 1 MNQ and the
gate reads per-contract economics, unchanged and still frozen.

Decision (spec owner): at funded deployment, size **3 micros per trade** —
the high-percentile-loss basis (~$100–120/micro against the worst
realistic 13-bar-exit loss), sitting between the catastrophic basis
(1 micro; disaster capped at $211 but a fifth of the earnings) and the
typical-loss basis (5 micros; one catastrophic-stop day costs 23% of a
standard $4,500 prop buffer). At 3 micros a catastrophic-stop day costs
≈ $633 — under 15% of the buffer — and expected earnings scale 3×
(today's replayed trade: +$747 at this size).

Standing check before funding: if the shadow's OBSERVED loss
distribution contradicts the ~$100–120/micro high-percentile assumption
(catastrophic stop firing more often than the develop record's rate, or
a fatter 13-bar loss tail), this policy is re-decided from the shadow
data before any funded order. That review is risk operations, not a
strategy-parameter change.

---

## Amendment — shadow sizing 1 → 4 MNQ (recorded 2026-08-19, spec owner)

**This amendment BREAKS the original sizing freeze**, by explicit
spec-owner decision made 2026-08-19 with the tradeoffs stated in
session (a sizing change buys no gate information — the gate is
per-contract — and voids the original window's admissibility). It is
recorded rather than hidden:

- **Change:** shadow `quantity` 1 → **4 MNQ**, constant. Effective the
  next session start (**2026-08-20**); not applied mid-session — the
  2026-08-19 session, including its open position, completed at 1 MNQ.
  Entry/exit/caps/backstops and every other declared parameter:
  unchanged.
- **Gate clock RESTARTS** at the first 4-MNQ session (2026-08-20):
  3 months or 60 trades, whichever is longer, from that date. The
  1-MNQ window (2026-08-14 → 2026-08-19, 5 closed trades, −$201.50
  net incl. one catastrophic stop) is spent evidence: recorded,
  reported alongside the new window, usable against the strategy but
  not for it.
- **Gate arithmetic, normalized:** the gate stays per-contract
  NQ-equivalent (EV ≥ +$20/trade NQ-equiv, PF ≥ 1.10). At 4 MNQ,
  NQ-equiv EV/trade = journal realised dollars × 2.5 (1 NQ = 10 MNQ).
  PF and the drift audit are size-invariant.
- **Risk note at this size:** a catastrophic-stop day ≈ $844 (4 ×
  ~$211/micro) — 18.8% of a $4,500 buffer, above the "under 15%" line
  that selected 3 micros in the 2026-08-14 funded-sizing addendum. The
  funded-stage policy (3 micros) is NOT changed by this amendment; if
  the intent is to fund at 4, that is re-decided at the pre-funding
  risk review with the shadow's observed loss distribution, per the
  standing check above.
