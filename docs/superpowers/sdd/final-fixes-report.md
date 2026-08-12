# Final whole-branch review: fixes report

Branch `main`, repo `/sessions/vibrant-tender-goldberg/work/nq-agent`. All seven findings fixed,
one commit per finding group, in the specified order (C-group first, then I1, I2, Minor1, Minor2).

## Status: all seven fixed

| Finding | Status | Commit |
|---|---|---|
| C1 (double-counted strategy state on resume) | Fixed | `17eed54` |
| C2 (post-resume_from bars dispatched as live) | Fixed | `17eed54` |
| C3 (restored position stop-tested against pre-entry bars) | Fixed | `17eed54` |
| I1 (trades_taken/is_halted never reset on rollover) | Fixed | `48e52bd` |
| I2 (silently refused entry recorded as opened) | Fixed | `70c882e` |
| Minor 1 (flush() fabricates closed=True) | Fixed | `eaa240c` |
| Minor 2 (typo'd config key silently reverts to default) | Fixed | `12c1eaa` |

Nothing was skipped or worked around. No BLOCKED conditions encountered.

## C1/C2/C3 — the warmup window (commit `17eed54`)

**Root cause confirmed**: `Engine.run` classified `bar.close_time <= resume_from` as "warmup" and ran
it through `strategy.on_bar`/`PositionTracker.on_bar` with only the resulting *signal* suppressed. But
those bars are already reflected in the restored `SessionState` (strategy_state, position, trades_taken).
Replaying them double-counts. Bars genuinely after `resume_from` had no separate window and were
dispatched as live immediately.

**Fix**: `Engine.run` now classifies every bar into three windows using two boundaries,
`resume_from` and a new `backfill_until: datetime | None` (default `None`, meaning no catch-up window
— the replay case):

1. `skip` (`close_time <= resume_from`): no `strategy.on_bar`, no `PositionTracker.on_bar`, no persist.
   `SessionManager.on_bar` still sees the bar (needed for session adoption; verified this is required —
   see below), but any signal it returns is discarded. A single `backfill_skipped` event is journaled
   once at the end of `run()` with a count, not one per bar.
2. `warmup` (`resume_from < close_time <= backfill_until`): strategy and tracker run for real, signals
   suppressed and journaled `signal_suppressed_backfill`, as before. Empty for `ReplayFeed` since it
   never sets `backfill_until` — a replay restart is a deterministic continuation, not a catch-up.
3. `live`: everything else.

`backfill_until` was added to `Engine.__init__` and to `run_from_config` (test-injectable, mirroring
the existing `strategy_override` pattern) since no live feed exists yet in this build to supply a real
one. `feed/base.py`'s `stream()` docstring was rewritten to describe the same three windows so the
`DataFeed` contract and the engine's classification agree.

Whether to still call `SessionManager.on_bar` during `skip` was explicitly checked, not assumed: without
it, a run whose entire `max_bars` budget lands inside the skip window would never call
`session.on_bar` at all, leaving `current_session_date` at `None` for the whole run and delaying
`session_resumed`/`session_start` indefinitely (or, in a fully-skip run, forever). Keeping the call (and
discarding any flatten signal it returns) is cheap and closes that gap; confirmed via
`test_restart_rebuilds_strategy_state` and the by-hand kill/restart run below, both of which depend on
`_adopt()` firing before the strategy is asked to do anything on the resumed session.

### Tests added (`tests/test_end_to_end.py`)
- `test_accumulating_strategy_crash_resume_matches_an_uninterrupted_run` — pins C1. A new
  `AccumulatingStrategy` (counts 1m bars, sums closes) run straight through vs. crashed-and-resumed
  (`max_bars=30` then unlimited) must produce identical `bars_seen`/`sum_close`. `AlwaysStrategy`'s
  `{"fired": bool}` cannot pin this — it's idempotent.
- `test_strategy_on_bar_is_not_called_for_bars_at_or_before_resume_from` — a `CallCountingStrategy`
  records every `on_bar` call's `close_time`; asserts none are `<= resume_from`.
- `test_a_restored_position_is_not_closed_by_bars_predating_its_own_entry` — pins C3. A 5-bar custom
  fixture (`delayed_entry_fixture`) where bar 1 dips to a low of 50 before a `DelayedEntryStrategy`
  opens LONG on bar 2 with stop 60. Crash after bar 4 (position still open), resume; asserts the
  persisted position survives and zero `position_closed` events exist anywhere in the journal.
- `test_bars_within_the_backfill_window_are_suppressed_not_dispatched` and
  `test_a_resumed_run_produces_no_suppressed_signals` — pin C2 and the replay case respectively, using
  an explicit `backfill_until` (no live feed exists to produce a real one).

### Mutation verification (reverted after, not committed)
Reintroduced the old single-predicate classification (`warmup = resume_from is not None and
bar.close_time <= resume_from`, no skip window, unconditional `tracker.on_bar`/`strategy.on_bar`) and
re-ran the new tests:

```
FAILED test_accumulating_strategy_crash_resume_matches_an_uninterrupted_run
  assert 445 == 420   (resumed run double-counted the pre-crash bars)
FAILED test_strategy_on_bar_is_not_called_for_bars_at_or_before_resume_from
  assert False        (on_bar was called for a bar at/before resume_from)
FAILED test_a_restored_position_is_not_closed_by_bars_predating_its_own_entry
  state.position is None   (bar 1's low=50 fabricated a STOP against a position
                             that didn't exist yet, 90s before its real entry)
FAILED test_bars_within_the_backfill_window_are_suppressed_not_dispatched
  assert 8 == 4        (bars inside the backfill window dispatched to the router)
FAILED test_a_resumed_run_produces_no_suppressed_signals
  assert not []   -> the replay case leaked suppressed-signal events too, under the old code
```
All five failed as expected; reverted the mutation, full suite back to green, diff confirmed
byte-identical to the pre-mutation state.

### Existing tests changed (premise invalidated, not just adjusted for taste)
- `test_backfill_signals_are_suppressed_and_journaled` asserted that a plain `ReplayFeed` resume
  produces `signal_suppressed_backfill` events. That assertion *was* C1/C2/C3 — it was pinning the bug.
  Replaced by `test_a_resumed_run_produces_no_suppressed_signals` (asserts the opposite, correctly) plus
  the `backfill_until`-based test above for the real window the original was trying to reach for.
- `test_warmup_suppression_blocks_dispatch_even_when_risk_would_allow_it`'s assertions still pass
  unchanged (the bar it exercises is now `skip`, not `warmup`, but both block dispatch) — only its
  docstring was rewritten, since the old one made a specific mutation-testing claim about the warmup
  `return` in `_handle_signal` that is no longer what blocks this particular scenario.

## I1 — rollover reset and is_halted restore (commit `48e52bd`)

`Engine._reset_after_rollover(prior_session_date)` compares `SessionManager.current_session_date`
before and after each call to `session.on_bar` (called from both the `skip` and `live`/`warmup`
branches of `run()`) and zeroes `trades_taken`/`is_halted` when it changed — but only when
`prior_session_date is not None`, so the engine's first-ever bar (cold start, or the
adopt/start that turns a resume into a running session) doesn't immediately clobber a value the
constructor just set correctly.

`Engine.__init__` gained an `is_halted: bool = False` parameter (previously hardcoded `False`
unconditionally); `run_from_config` now reads `prior.is_halted` alongside the other restored fields.

### Tests added (`tests/test_end_to_end.py`, new `two_session_fixture` helper — two consecutive
trading days, single continuous run, no crash/resume involved)
- `test_trades_taken_resets_on_a_session_rollover` — `AlwaysStrategy` (re-arms every session) with
  `max_trades_per_day: 1`; asserts day 2's entry is accepted, not MAX_TRADES-vetoed by day 1's count.
- `test_is_halted_resets_on_a_session_rollover` — `ExplodingStrategy` raises once (3rd `on_bar` call
  ever) on day 1; asserts `engine.is_halted is False` by the end.
- `test_is_halted_is_restored_on_resume` — a halted run followed by a resumed one; asserts the resumed
  engine is halted too.

### Mutation verification (reverted after)
Neutered `_reset_after_rollover`'s condition (`if False and ...`) and hardcoded
`is_halted = False  # never restored` in `run_from_config`:
```
FAILED test_trades_taken_resets_on_a_session_rollover
FAILED test_is_halted_resets_on_a_session_rollover
  assert True is False   (is_halted stayed True into day 2)
FAILED test_is_halted_is_restored_on_resume
  assert False is True   (resumed engine came back up un-halted)
```
All three failed as expected; reverted, full suite back to green, diff confirmed identical.

## I2 — observable entry refusal (commit `70c882e`)

`PositionTracker.on_signal` now returns `Position | None` (the opened Position, or `None` if refused —
wrong intent, or a position already open) instead of implicit `None` always. `Engine._handle_signal`
journals `position_open_rejected` with `reason="position already open"` and does **not** increment
`trades_taken` when refused; only increments and journals `position_opened` when the tracker actually
opened something.

### Tests added
- `tests/test_position.py`: `on_signal` returns the opened `Position` on success, and returns `None`
  (not the pre-existing position) on refusal, without touching what's already open.
- `tests/test_end_to_end.py::test_a_refused_second_entry_is_journaled_and_not_counted_as_a_trade` — a
  new `LongThenShortStrategy` fires LONG then SHORT one bar apart (`max_bars=5`, well clear of cutoff).
  Asserts exactly one `position_opened`, exactly one `position_open_rejected`, both signals still
  dispatched (8 `order_result` records — risk and the router have no notion of the local position),
  `trades_taken == 1`, and the persisted position is still the original LONG.

### Mutation verification (reverted after)
Reinstated the engine's old unconditional `trades_taken += 1` / `position_opened` write:
```
FAILED test_a_refused_second_entry_is_journaled_and_not_counted_as_a_trade
  assert 2 == 1   (two position_opened records — the exact measured bug)
```
Separately, made `on_signal` return `self._position` (the pre-existing position) instead of `None` on
refusal:
```
FAILED test_on_signal_returns_none_when_a_position_is_already_open
```
Both failed as expected; reverted, full suite back to green.

## Minor 1 — flush() partial buckets (commit `eaa240c`)

`BarAggregator.flush()` now compares each bucket's `close_time` against `self._last_ts` and marks
`closed=False` when no tick reached it (still returns the bar — a caller that wants the partial can
still have it). `ReplayFeed.stream()` and `ReplayFeed.get_bars()` (both documented "closed bars only")
filter out anything `flush()` marks not closed.

This exposed a real gap in `tests/fixtures/2026-07-15.jsonl`: its last tick (20:29:55) is five seconds
short of ever closing the 20:29-20:30 bucket, so the fixture never actually reached the 16:30 session
cutoff — previously masked by `flush()` fabricating a closed bar there. `make_fixture.py` now appends
one closing tick at the session's true end (20:30:00 UTC). Verified this is the *only* change to the
regenerated fixture: `diff` shows exactly one line appended, all 5040 existing lines byte-identical.
Three tests turned out to depend on that fabricated bar without it being their stated purpose —
`test_cutoff_flatten_is_routed_when_a_position_survives_to_the_close`,
`test_stream_advances_the_sim_clock_to_each_bar_close` (both now pass for the right reason), and
`test_final_partial_bucket_is_flushed`, renamed `test_the_sessions_final_bar_reaches_the_true_close`
since nothing is flushed to produce it anymore. `tests/test_end_to_end.py`'s own hand-built
`two_minute_session_settings` fixture had the identical one-tick-short gap and needed the same fix for
`test_a_stop_hit_on_the_cutoff_bar_exits_via_stop_not_flatten`.

### Tests added, both halves as specified
- `tests/test_aggregator.py`: a lone partial bucket is marked `closed=False` (still returns OHLCV);
  every timeframe's bucket is marked independently, not just a special-cased single one; a bucket that
  just rolled over via `add_tick` (`closed=True`) is not confused with the fresh bucket that rollover
  opened (still `closed=False` from `flush()`).
- `tests/test_replay_feed.py::test_a_bucket_left_open_by_the_last_tick_is_not_yielded` — a 2-tick
  fixture ending mid-minute; asserts `stream()` yields nothing.

### Mutation verification (reverted after)
Reinstated `flush()`'s hardcoded `closed=True`:
```
FAILED test_flush_marks_a_genuinely_partial_bucket_as_not_closed
FAILED test_flush_marks_every_timeframes_partial_bucket_as_not_closed
FAILED test_flush_after_a_rollover_still_reports_the_new_bucket_as_partial
FAILED test_a_bucket_left_open_by_the_last_tick_is_not_yielded
```
All four failed. Separately, with the aggregator fix left intact, removed `ReplayFeed.stream()`'s
`if not bar.closed: continue` filter:
```
FAILED test_a_bucket_left_open_by_the_last_tick_is_not_yielded
  (bar leaked through with closed=False)
```
Confirms the `ReplayFeed`-level test pins the filter specifically, not just the aggregator's marking.
Both mutations reverted, full suite back to green, diffs confirmed identical.

## Minor 2 — unknown config keys (commit `12c1eaa`)

`load_settings` now calls `_validate_known_keys(data, Settings, section="<top level>")` after the
base.yaml/override merge and before `Settings(**data)`. It walks the merged dict against
`Settings.model_fields`, recursing into fields whose annotation is itself a `BaseModel` (`session`,
`context`, `risk`, `router`) or a `list` of one (`executors`), raising `ValueError` naming the unknown
key and the section. `extra="ignore"` on `Settings` itself is untouched — env-var layering
(`NQ_`-prefixed, `__`-nested) still works exactly as before, since this validates only the YAML-sourced
dict, not the env-var-merged `Settings` construction.

### Tests added (`tests/test_config.py`)
- Unknown top-level key, unknown key nested under `risk:`, unknown key inside an `executors:` list
  item — all rejected, message contains both the key and the section.
- A correctly-spelled `risk.max_trades_per_day` still loads (guards against over-strictness).

### Mutation verification (reverted after)
Removed the `_validate_known_keys` call (`Settings(**data)` called directly on the merged dict — the
exact prior behaviour):
```
FAILED test_unknown_top_level_key_is_rejected           - DID NOT RAISE
FAILED test_unknown_nested_key_is_rejected               - DID NOT RAISE
FAILED test_unknown_key_in_an_executor_list_item_is_rejected - DID NOT RAISE
```
All three failed as expected; the fourth (valid config still loads) correctly kept passing either way.
Reverted, full suite back to green.

## Test suite progression

| After | Passing | Delta |
|---|---|---|
| Baseline (before any fix) | 185 | — |
| C1/C2/C3 (`17eed54`) | 189 | +4 (net: −1 replaced, +5 new) |
| I1 (`48e52bd`) | 192 | +3 |
| I2 (`70c882e`) | 195 | +3 |
| Minor 1 (`eaa240c`) | 199 | +4 |
| Minor 2 (`12c1eaa`) | 203 | +4 |

All 185 pre-existing tests still pass, except the 2 explicitly documented above whose premise the fix
inverted (`test_backfill_signals_are_suppressed_and_journaled`, replaced;
`test_final_partial_bucket_is_flushed`, renamed with an unchanged assertion plus one more). Every other
addition is new.

## Lint and type checks (final state, all seven fixes applied)

```
$ python3 -m ruff check .
All checks passed!

$ python3 -m mypy src/nq_agent
Success: no issues found in 26 source files

$ python3 -m mypy --strict src/
Success: no issues found in 26 source files
```

## By-hand definition-of-done verification

Ran from a clean `var/` (removed before each scenario below; `var/` is gitignored and not part of the
commits).

### Plain run
```
$ rm -rf var && PYTHONPATH=src python3 -m nq_agent --config config/paper.yaml \
    --replay tests/fixtures/2026-07-15.jsonl
2026-08-11 09:14:10 INFO dry run notify dryrun_notify {'intent': 'ENTRY', 'direction': 'LONG', ...}
2026-08-11 09:14:10 INFO dry run execute dryrun_broker:tradeify {...}
2026-08-11 09:14:10 INFO dry run execute dryrun_broker:mff {...}
2026-08-11 09:14:10 INFO dry run execute dryrun_broker:fundednext {...}
EXIT CODE: 0
```
Journal: `{'order_result': 4, 'session_start': 1, 'signal_emitted': 1, 'position_opened': 1,
'position_closed': 1, 'session_end': 1}`. One ENTRY, dispatched to all four executors, position closed
before end of session, clean exit.

### Kill/restart
```
$ rm -rf var
$ PYTHONPATH=src python3 -m nq_agent --config config/paper.yaml \
    --replay tests/fixtures/2026-07-15.jsonl --max-bars 60
  ... ENTRY dispatched to all 4 executors ...
EXIT CODE RUN1: 0
  state after run 1: trades_taken=1, is_halted=false, position=null (stopped/targeted out within
  the first 60 bars), last_bar_time=2026-07-15T14:20:00Z

$ PYTHONPATH=src python3 -m nq_agent --config config/paper.yaml \
    --replay tests/fixtures/2026-07-15.jsonl
  (no output -- nothing new to dispatch)
EXIT CODE RUN2: 0
```

Combined journal across both runs (12 events total):
```
Counter({'order_result': 4, 'session_end': 2, 'session_start': 1, 'signal_emitted': 1,
         'position_opened': 1, 'position_closed': 1, 'session_resumed': 1, 'backfill_skipped': 1})

ENTRY signal_emitted count across both runs: 1
  2026-07-15T13:31:00+00:00  (single signal_id, from run 1 only)

session_resumed count: 1
session_start count: 1
backfill_skipped events: [{'count': 60}]   (run 2 correctly skipped, not replayed, the 60
                                             pre-crash bars, journaled once)

final state: trades_taken=1, is_halted=false, position=null, last_bar_time=2026-07-15T20:30:00Z
```

Confirmed: **exactly one ENTRY `signal_emitted` across both runs**, and **`trades_taken` is 1, not
2**. `order_result` count is 4, not 8 — no double dispatch. `session_start` fired once (run 1 only),
`session_resumed` fired once (run 2 adopted rather than restarting) — session adoption survives the new
skip-window handling. Definition of done holds.

`var/` removed after verification; `git status` clean; working tree matches the five commits below with
nothing else pending.

## Commits

```
17eed54 fix: replace the warmup window with skip/warmup/live, per resume_from and backfill_until
48e52bd fix: reset trades_taken/is_halted on session rollover; restore is_halted on resume
70c882e fix: make PositionTracker.on_signal's refusal observable
eaa240c fix: flush() reports genuinely partial buckets as closed=False
12c1eaa fix: reject unknown config keys instead of silently falling back to defaults
```

Diffstat across all five: 13 files changed, 1055 insertions(+), 36 deletions(-).

## Concerns / notes for the next reviewer

- Context (`src/nq_agent/context.py`) bar history is not populated during the `skip` window by design
  choice, not oversight: the C1/C2/C3 fix's instructions list exactly three things to withhold from a
  skipped bar (strategy call, position tracking, persist), and Context was deliberately left out of that
  list since it is not part of the persisted `SessionState` and does not participate in any of the three
  double-counting bugs. A strategy that reads multi-bar lookback (`context.bars(...)`) will see a short
  history immediately after a resume until enough live/warmup bars accumulate. No shipped strategy uses
  lookback and no required test exercises it either way, so this was not changed — flagging it as a
  known, pre-existing limitation (Context history was never part of the persistence contract before this
  fix either) rather than a new gap.
- The multi-day resume+replay edge case (a feed that would replay bars from a day *before* the resumed
  session alongside the resumed day itself) has a latent sharp edge in `SessionManager`'s adopt-vs-start
  logic that predates this fix and is out of scope for the seven findings: the manager's `_resume_pending`
  match is exact-date, so a hypothetical earlier day's first bar would trigger a normal `_start()`
  (wiping restored strategy state meant for the *later*, resumed day) before the resumed day is ever
  reached. `ReplayFeed` always replays a single fixture from its own start, and no required scenario
  spans a resume across multiple session dates in one feed, so this was not exercised or touched.
