# SDD progress ledger

Repo: /sessions/vibrant-tender-goldberg/work/nq-agent (sandbox; synced to Desktop after each task)
Plan: docs/superpowers/plans/2026-08-09-nq-agent-scaffolding.md

Task 1: complete (commits 22882eb..5b76b82, review clean)
  Fixes applied during review: ruff target-version py312 -> py310 (was rejecting the three
  mandated 3.10-compatible spellings via UP042/UP017/UP041); froze SessionConfig/ContextConfig/
  RouterConfig/ExecutorConfig/Settings, RiskConfig stays mutable by design.
  Minor findings deferred to final review triage:
    - no regression test asserting config models are frozen
    - tests/test_config.py:7 unused tmp_path fixture param (brief-verbatim)
    - Settings extra="ignore" silently drops misspelled YAML/env keys (brief-verbatim)
    - config.py BASE_CONFIG is CWD-relative, fragile if invoked outside repo root (brief-verbatim)
    - RiskConfig lacks validate_assignment=True; kill_switch_path mutation bypasses coercion
  Open for Jay: `uv venv --python 3.12 && uv pip install -e ".[dev]"` is untested (no 3.12 here).
Task 2: complete (commits d9bb55a..HEAD, review clean)
  NOTE: original implementer subagent was killed mid-run by a session limit. Controller
  recovered the uncommitted work, verified it, and committed. TDD evidence unrecoverable
  for this task only; reviewer was told to judge code on merits instead.
  Fixes during review: SessionState.last_bar_time accepted naive datetimes -> replaced five
  hand-written _utc validators with one UtcDatetime = Annotated[datetime, AfterValidator(...)]
  used by all six datetime fields; replaced a noqa-suppressed unused Tick import with two real
  Tick tests; added non-UTC-offset conversion coverage.
  Minor findings deferred to final review triage:
    - no exhaustiveness guard on Direction branches in Signal._check_intent_prices
    - two ENTRY-missing-one-price permutations and FLATTEN-single-stray-price untested
    - Bar.closed=False untested (exercised in Task 5)
    - str(Direction.LONG) is "Direction.LONG"; json.dumps/pydantic give "LONG" correctly
Task 3: complete (commits adcfdf2..HEAD, review clean, no Critical/Important)
  Reviewer independently reproduced the DST mutation test (fixed-EDT and fixed-EST mutants each
  fail exactly 4 of 12 tests) and recomputed all 13 asserted UTC instants from stdlib zoneinfo.
  Fixes after review: added tzdata dependency (Windows/minimal-container portability),
  asyncio_default_fixture_loop_scope to silence a pytest_asyncio deprecation, three SimClock
  naive-datetime tests.
  Deviation from brief: two 103-char assert lines in the DST-stability test exceeded ruff's
  100-char limit; extracted edt_instant/est_instant locals, values unchanged.
Task 4: complete (commits 37704fe..HEAD, review clean after fixes)
  Reviewer found four Important defects in the plan's own journal code, all reproduced concretely:
  payload could overwrite the clock-sourced ts; naive datetimes accepted; nested models rendered
  datetimes as RFC3339 "Z" which datetime.fromisoformat cannot parse before 3.11 while bare
  datetimes rendered "+00:00"; NaN/Infinity produced invalid JSON. Fixed via reserved-key guard,
  shared public models.require_utc, model_dump(mode="python"), allow_nan=False.
  Re-review verified fixes against 3-level nesting, non-UTC offsets and real production models.
  Minor findings deferred to final review triage:
    - RESERVED_KEYS "event" entry is unreachable (Python raises TypeError first)
    - _encode's Enum branch is dead code for all current (str, Enum) types, untested
Task 5: complete (commits effab0b..HEAD, two Important findings fixed, mutation-verified)
  Reviewer proved the ordering tie-break was untestable via the public API (mutating the sort key
  left all 12 tests green). Fixed with a white-box test on _ordered; re-verified by mutation.
  Also: BarAggregator now rejects duplicate timeframes (previously doubled that timeframe's volume).
  NOTE: these two fixes were controller-made and mutation-verified rather than re-reviewed by a
  subagent; the final whole-branch review is the gate on them.
  Minor deferred to final review triage:
    - flush()-forced partial-window bars carry closed=True, indistinguishable from natural closes
Task 6: complete (commits 448646a..4973df4, review clean, no Critical/Important)
  Reviewer independently verified get_bars/stream field-for-field agreement, SimClock
  monotonicity landing exactly on the last close, resume_from inertness across 5 values,
  the ordering guarantee across all 84 tie groups of a 3-timeframe stream, and fixture
  determinism (md5 e15012af...). Only deviation: one 101-char test line reflowed.
  Minor findings deferred to final review triage:
    - `assert all(bar.closed ...)` is tautological; aggregator._finish hardcodes closed=True,
      so no test could catch an open-bucket leak that way (ordering/OHLCV checks do)
    - get_bars/stream take a `symbol` argument decoupled from the constructor's; passing a
      mismatched symbol silently relabels NQ fixture bars. Dormant, plan-mandated.
    - get_bars re-aggregates the whole fixture per call, filtering after. Fine at 5040 lines.
    - 1h buckets are epoch-aligned, so with this 13:30-open fixture a trailing 1h bucket
      flushes at 21:00 UTC, past the 20:30 session close. Relevant to Task 12 if 1h is used.
Task 7: complete (commits 4973df4..HEAD, Important + Minors fixed)
  Fixed: Context.bars(tf, 0) returned the ENTIRE history (list[-0:] is list[0:]);
  Context now rejects history_bars < 1 (deque(maxlen=0) silently ate every bar);
  Strategy.restore_state / on_session_start docstrings now document the resume ordering.
  *** BLOCKING NOTE FOR TASKS 12 AND 14 ***
  on_session_start unconditionally clears what restore_state sets. The plan's drafted
  run_from_config restores state BEFORE the engine loop, and SessionManager.on_bar calls
  on_session_start on the first bar of any freshly constructed manager - including a resume.
  So the restore is silently undone. The crash-recovery test still passes, but only by
  accident: the re-armed strategy fires on the first backfill bar, that signal is suppressed
  as warmup, and _fired ends up True again. Task 12/14 must handle resume explicitly.
  Minor deferred: AlwaysStrategy with zero/negative offsets raises from on_bar, not __init__.
Task 8: complete (commits f99ee5d..HEAD, two Important findings fixed)
  Collapsed the duplicated execute bodies into _dry_run_result; the reviewer correctly noted
  the "they'll diverge later" defence fails because the real executor/notifier are new sibling
  classes, not these growing apart. Fixed `if account_id` truthiness dropping the name suffix
  for account_id="".
  *** BLOCKING NOTE FOR TASK 9 ***
  DryRunExecutor(account_id=None).name and DryRunNotifier(name).name both reduce to the bare
  name, so two same-named instances are indistinguishable in OrderResult.executor_name - the
  only field identifying which destination produced a result. Router must reject duplicate
  executor names at construction.
Task 9: complete (commits 5b8af1f..HEAD, one Important finding fixed)
  Reviewer empirically confirmed on this interpreter that asyncio.TimeoutError is NOT the
  builtin TimeoutError on 3.10, and that router.py catches the right one - the highest-risk
  item in this component. Also verified true broker concurrency by wall-clock (4x0.3s in
  0.304s) and notify-first ordering by mutation.
  Fixed: no test caught a regression to sequential broker execution (proven by mutation).
  Added a rendezvous-based test; BaseException fallback now records latency_ms.
  Added beyond the brief, deliberately: Router rejects duplicate executor names.
  Minor deferred to final review triage:
    - the `if result.latency_ms: return result` passthrough is uncovered
    - external cancellation mid-dispatch surfaces as a failed OrderResult (Task 14 shutdown)
Task 10: complete (commits 317d008..HEAD, one Important finding fixed)
  Both the implementer and the reviewer independently mutation-tested the FLATTEN bypass;
  the implementer found the brief's 12 tests missed a reordered duplicate check and added
  test_flatten_bypasses_even_a_pending_duplicate, which the reviewer reproduced.
  Fixed: AccountRegistry used bool(value), so `account: "false"` ENABLED the account -
  wrong failure direction on a control that exists to stop trading. Now rejects non-booleans.
  Also prune _recent in record_accepted so the bound does not rely on caller discipline.
  Minor deferred: duplicate-window prune is one-sided; an out-of-order future entry would
  falsely veto an earlier signal. Safe direction, and time is monotonic by construction.
Task 11: complete (commits 346bece..112e602, review clean, no Critical/Important)
  Implementer mutation-tested its own work and found 3 real gaps in the brief's 12 tests
  (exact-touch boundaries, on_bar symbol-mismatch guard, restore()+exit preserving entry
  details); reviewer independently reproduced all 5 mutations plus 2 of its own.
  Minor deferred to final review triage:
    - on_signal's `intent is not ENTRY` guard is unpinned (dropping it passes all 15 tests);
      fallback is a loud AssertionError, and the engine only calls it with ENTRY
    - a bar that gaps through the stop fills at the stop price, not worse. Matches the spec,
      but it is the one optimistic edge the "stop wins" pessimism does not cover.
Task 12: complete (commits 112e602..4f44431, review clean, no Critical/Important)
  Required addition implemented: SessionManager takes resumed_session_date and adopts a
  matching first-bar session WITHOUT calling on_session_start (which would wipe restored
  strategy state), journaling session_resumed instead. Verified genuinely one-shot.
  Reviewer reproduced all 8 mutation claims and wrote two independent probes of its own.
  *** BLOCKING NOTE FOR TASK 14 ***
  run_from_config must compute and pass resumed_session_date=prior.session_date (a date,
  not a datetime) when resuming, or the resume adoption never engages.
  Minor deferred: no test asserts the signal_emitted journal record's field contents.
Task 13: complete (commits 4f44431..14d3145, review clean, no Critical/Important)
  Reviewer inspected the raw on-disk SQLite payload and confirmed Decimal is stored as a
  quoted JSON string, so float truncation is structurally impossible; reproduced all three
  mutation claims and stress-tested 100 concurrent writers.
  Minor deferred to final review triage:
    - SessionState.strategy_state is dict[str, Any]; Decimal/datetime values inside it come
      back as str. Dormant (AlwaysStrategy stores a bool, restore_state coerces defensively)
      but future strategy authors must not assume type fidelity there.
    - sqlite3 connections rely on refcount GC rather than explicit close. Verified benign.
Task 14: complete (commits 14d3145..e12c44d, review clean, no Critical/Important)
  Implementer found two real bugs by mutation: the resumed_session_date gap (the brief's own
  12 tests pass with it deleted, masked by warmup suppression) and a duplicate signal_emitted
  per cutoff flatten. Reviewer reproduced all four mutations on scratch copies.
  Controller verified the definition of done by hand: 1 ENTRY across a kill+restart,
  session_resumed journaled, trades_taken 1 not 2, notify journaled before the three brokers.
  Minor deferred to final review triage:
    - is_halted is not persisted; a strategy that halted before a crash is retried on restart
    - no test covers "halted mid-session with an open position" + cutoff flatten together
    - a resume crossing cutoff double-journals a flatten (signal_emitted + suppressed_backfill)
    - --max-bars is a clean stop, not a true SIGKILL; resume correctness does not depend on it

=== FINAL WHOLE-BRANCH REVIEW (opus) + FIX WAVE ===
Found 3 Critical, 9 Important, ~20 Minor. All three Criticals shared one root cause: the
resume warmup window was the COMPLEMENT of the designed one. State was restored covering
[session start, last_bar_time], then that same window was replayed as warmup - so an
accumulating strategy double-counted (measured 75 bars where an uninterrupted run saw 50),
and a restored position was stop-tested against bars predating its own entry (measured: a
fabricated STOP dated 28 minutes before entry, leaving the agent flat while the broker held
the position). Meanwhile the actual downtime window was classified LIVE and dispatched.
185/185 tests passed against all of it.
Fixed (commits 17eed54, 48e52bd, 70c882e, eaa240c, 12c1eaa): three explicit windows
(skip / warmup / live) with a backfill_until parameter; trades_taken and is_halted now reset
on session rollover and is_halted is restored from persisted state; PositionTracker.on_signal
returns the opened Position so a refused entry is journaled rather than silently counted;
flush() marks partial buckets closed=False and ReplayFeed.stream drops them; load_settings
rejects unknown config keys.
203 tests pass. Controller independently re-verified C1 with a control-vs-resume experiment
on an accumulating strategy (identical state), plus the definition of done and kill/restart.
NOT DONE: no second whole-branch review after this fix wave.
STILL OPEN (see final review, triaged "soon"): Executor has no close() hook; Executor's
required attributes are unenforced; run_from_config hardcodes ReplayFeed/SimClock so there is
no seam for a live feed; Engine.run teardown is not in a finally; required_timeframes is never
checked against config; Strategy.restore_state's docstring contradicts the engine's actual
adoption path; a strategy is still registered by editing a dict in main.py.
