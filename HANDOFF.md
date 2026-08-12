# NQ Agent — scaffolding handoff

The scaffolding is complete and the definition of done holds. 203 tests pass, `ruff check` and `mypy --strict src/` are clean.

## First: get the git history

The build ran in a Linux sandbox because git could not operate reliably against this folder from there — two stale lock files are still sitting in `.git/`. The full 34-commit history is in `nq-agent-git-history.bundle`. To adopt it:

```
cd "C:\Users\VYRA\Desktop\Auto Trading\nq-agent"
del .git\HEAD.lock .git\index.lock
git fetch nq-agent-git-history.bundle main:bundled-main
git reset --hard bundled-main
```

The working files on disk are already the final state, so the reset is a no-op on content — it just attaches the history.

## Run it

```
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl
```

That runs a full simulated session: `AlwaysStrategy` emits one entry, it routes to a dry-run notifier and three dry-run broker instances (one per prop account), the journal lands in `var/journal/2026-07-15.jsonl`, and state persists to `var/state.db`.

To prove crash recovery:

```
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl --max-bars 60
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl
```

Exactly one `ENTRY` across both runs, `trades_taken` stays 1, and the journal shows `session_resumed`.

**One thing I could not verify:** the sandbox only had Python 3.10, so `requires-python = ">=3.12"` and the `uv` install path are untested. The code is written in 3.10-compatible spellings deliberately (`class Foo(str, Enum)` not `StrEnum`, `timezone.utc` not `datetime.UTC`, `except asyncio.TimeoutError` not bare `TimeoutError`) — all upward-compatible, and ruff's `target-version = "py310"` is set to permit them. Don't let a linter "modernise" those back.

## Adding your real strategy

Implement `Strategy` in `src/nq_agent/strategy/`, then add one line to `STRATEGIES` in `main.py`. That is the whole job. Read `strategy/always.py` as the worked example.

Three things the interface does not tell you, which you should know before writing anything stateful:

1. **`get_state`/`restore_state` round-trip through JSON.** `Decimal` and `datetime` values inside `strategy_state` come back as strings. Coerce defensively in `restore_state`, the way `AlwaysStrategy` does.
2. **`required_timeframes` is declarative only.** Nothing checks it against `config`'s `timeframes`. If you declare `15m` and the config says `[1m, 5m]`, your strategy silently never fires.
3. **`on_bar` is sync `def` on purpose.** You cannot await anything, which is the point — a strategy that can't do I/O is a strategy that replays deterministically.

## Known gaps, in the order I'd close them

These came out of the final whole-branch review and are all triaged as "worth doing soon", not blockers for scaffolding — but the first three matter before a live executor exists.

1. **`Executor` has no `close()` hook.** `DataFeed` has one and the engine calls it; `Executor` doesn't. A real webhook executor holding an `aiohttp.ClientSession` has nowhere to clean up.
2. **`run_from_config` hardcodes `ReplayFeed` and `SimClock`.** Wiring `DatabentoFeed` means editing that function, not just implementing `DataFeed`. It also keys the state lookup off the replay's *first* tick, so a multi-day file can't resume its later sessions.
3. **`Engine.run`'s teardown isn't in a `finally`.** A feed error mid-stream skips `end_session()` and `feed.close()`. Nothing writes the `feed_error` event the design names.
4. `Executor`'s `name`/`account_id`/`enabled` are bare annotations, not enforced — a subclass that forgets them fails with `AttributeError` at dispatch time.
5. An external cancellation mid-dispatch surfaces as a failed `OrderResult`. With real orders, "failed" and "unknown, possibly filled" must not be the same value.
6. `Strategy.restore_state`'s docstring describes an ordering rule the engine doesn't actually follow — correctness comes from `SessionManager` adopting a resumed session instead. Rewrite it to match reality.
7. A bar that gaps through the stop fills *at* the stop price, not worse. It's the one optimistic edge the "stop wins" pessimism doesn't cover; record it before anyone reads P&L off a backtest built on this.
8. `RiskManager._recent` isn't persisted, so the duplicate-signal window is empty for 60s after a restart.

## What the final review caught that the tests didn't

Worth reading `docs/superpowers/sdd/progress.md` before you trust anything here. The short version: after all 14 tasks were built and individually reviewed with 185 passing tests, a whole-branch review found three Critical defects that every one of those tests missed. They shared one root cause — the resume warmup window was the exact complement of the designed one, so a restored strategy replayed the bars its own state already included, while the real downtime window was treated as live and dispatched to brokers. An accumulating strategy double-counted; a restored position got stopped out by a bar 28 minutes older than its own entry.

`AlwaysStrategy` survived it only because `{"fired": bool}` is idempotent under double application. Your real strategy would not have been.

That is fixed, verified by a control-vs-resume experiment on a deliberately accumulating strategy. The lesson worth keeping: a reference strategy simple enough to always pass is a reference strategy that proves less than it appears to.

**Not done:** there was no second whole-branch review after that fix wave. Worth one before you build on it.
