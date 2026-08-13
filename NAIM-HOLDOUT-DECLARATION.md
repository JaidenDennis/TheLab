# NAIM holdout declaration — committed BEFORE the read

Per spec NAIM v1.0 §9.2: one variant, one read, declared first.

**Declared variant: `core`** — the §9.4 sanity-anchor configuration, i.e.
the published replication's own settings: L=90, 30-minute trigger
boundaries, no OFI gate, close-based band+VWAP structural stop,
catastrophic stop 80 pts, entry window 09:31–15:00, cap 4/day, FOMC
calendar populated, hold-to-close posture, 1 contract flat, $10/RT + 1-tick
adverse entry.

**Develop gate (pre-registered §9.2), evaluated 2026-08-13 on
2020-07-01 → 2024-09-30:**

| criterion | bar | core | pass |
|---|---|---|---|
| Net EV/trade | ≥ +$40 | +$206.23 | ✓ |
| Years positive | ≥3 of 4 | 5 of 5 (incl. partial 2020) | ✓ |
| Profit factor | ≥ 1.15 | 1.48 | ✓ |
| Trades/month (median ≥8) | ≥8 | 18.0 mean | ✓ |

**Declaration context:** the §10 variant matrix is still running and NO
variant result beyond `core` has been seen at declaration time. Whatever
the matrix later shows is descriptive; no variant may claim this holdout.
A better-looking variant would need its own out-of-sample period, which
only time can supply.

**Holdout:** 2024-10-01 → 2026-08-12, seeded walk-forward from the
develop-run terminal state, read exactly once, immediately after this
file's commit.

**Pre-registered holdout gate:** net EV ≥ +$20/trade AND PF ≥ 1.10 over
the full holdout.
