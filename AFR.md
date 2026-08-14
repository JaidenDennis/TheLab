# AFR — Absorption Flow Reversal: Final Report

**Status: DEAD — killed cleanly by its own pre-registered control design,
in one develop pass, with zero data cost and no cycles spent. Every
absorption cell is negative; so is every naive-fade control cell. The
pre-committed interpretation for that outcome (spec §6.1): "absorption
doesn't rescue fading and AFR dies cleanly." It did.**

| | |
|---|---|
| Spec | AFR v1.0, program 7 — fade absorbed aggression (flow-triggered MR) |
| Data | The NQ tick year via existing TFR decision files — $0 |
| Costs | $10/RT + 1-tick adverse, 1 NQ contract, house standard |
| Machinery | `strategy/afr.py` (13 tests, 4 mutations caught), `scripts/run_afr.py`, 9 pre-declared cells |
| Run date | 2026-08-14 |

## 1. The frequency finding (before any backtest ran)

The spec's §4 pre-check, executed first: true absorption — |F1_5| ≥ Q70
with displacement suppressed ≥ 1σ — occurred **10 times in the entire
year**; at 1.5σ, **zero**. Aggressive flow on NQ almost always moves
price. The develop matrix therefore ran at the only frequency-viable
declared grid point (E0.5, 33/month), with the deep-tail cell kept for
the record.

## 2. The matrix (all 9 pre-declared cells)

| cell | n | net $ | EV/trade | win% | PF |
|---|---|---|---|---|---|
| afr_t3 | 305 | −7,075 | **−23.20** | 42.3% | 0.95 |
| afr_t6 | 301 | −28,350 | −94.19 | 36.9% | 0.74 |
| afr_t13 | 295 | −32,940 | −111.66 | 34.9% | 0.71 |
| afr_norm | 304 | −9,145 | −30.08 | 44.4% | 0.93 |
| naive_t3 (control) | 600 | −43,030 | −71.72 | 45.0% | 0.86 |
| naive_t6 (control) | 600 | −51,735 | −86.22 | 41.2% | 0.85 |
| naive_t13 (control) | 599 | −49,135 | −82.03 | 39.1% | 0.87 |
| naive_norm (control) | 600 | −46,745 | −77.91 | 47.3% | 0.85 |
| afr_t6 @ E1.0 (record) | 10 | −8,185 | **−818.50** | 10% | 0.06 |

## 3. What the design measured (all pre-registered readings)

1. **The mechanism is real but insufficient.** At matched exits,
   absorption improves the fade by ≈ +$48–49/trade over the naive control
   (t3: −$23 vs −$72; norm: −$30 vs −$78). The eff condition carries
   information — just not enough to make fading this tape positive.
2. **The naive-fade control replicated the external falsification** on
   our own pipeline: −$72 to −$86/trade across every exit, squarely in
   Mesfin's naive-fade-loses territory. The control behaved exactly as
   the literature predicted, which is what makes the AFR reading
   trustworthy.
3. **Longer holds bleed more** (t3 −$23 → t13 −$112): faded flow tends to
   continue, not revert — the mirror image of fc_t13's hold-duration law,
   from the opposite side of the same tape.
4. **Deep absorption is a continuation signal, not a reversal signal**:
   the spec-default tail (E1.0, n=10) lost −$818/trade at a 10% win rate.
   The rare occasions when heavy flow truly fails to move price preceded
   *more* of the same direction, not reversal.
5. **The portfolio claim dies with the program**: daily P&L correlation
   with fc_t13 was 0.061 over 133 common days — uncorrelated rather than
   anti-correlated, and moot given the sign.
6. Quarters: 1 of 4 positive (2026Q2 only). Permutation control: not run
   — moot below a zero-EV result, per protocol.

## 4. Verdict

Dead as specified, first pass, no rescue attempted: the fade side of NQ
intraday flow is structurally unprofitable at these costs even with the
absorption condition doing real work. The reversal-side family (AFR now;
DDR's stage-1 fail in FRB-1) is closed unless a fundamentally different
mechanism arrives with its own program. What survives: the measured
+$48/trade value of the absorption feature (a candidate *veto* input for
momentum systems — the F3 story returns to its original role), and one
more confirmation that this tape pays continuation, not reversion.

## 5. Reproduce

```
uv run python scripts/run_afr.py --fixtures var/fixtures/1m \
    --decisions var/decisions/k3m --out var/afr --variant <cell>
```

Cells: `scripts/run_afr.py::build_variants`. Frequency pre-check and all
counts derive from the decision files under var/decisions/k3m.
