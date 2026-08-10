# NQ Agent — Scaffolding Design

Date: 2026-08-09
Status: Approved

## Purpose

Build every layer around the strategy so that adding rules later means implementing one interface and nothing else. When this design is implemented, the system runs end to end on a stub strategy that never fires, and a paper strategy that fires on a trivial condition, proving the whole pipeline works before real rules exist.

## Non-goals

Out of scope. Do not drift into these:

- Any real strategy logic
- Backtesting engine
- The LLM judgment filter
- Chrome MCP automation
- Live money

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Concurrency | Async throughout | Network feed plus concurrent executor fan-out is asyncio's exact job |
| `Strategy.on_bar` | Sync `def` | Structurally cannot await network I/O; purity enforced by types |
| Python | 3.12 | `asyncio.TaskGroup`, `datetime.UTC`, `tomllib`, databento wheels present |
| Package manager | uv | Fast, lockfile, single tool for venv and deps |
| Models | Pydantic v2 | Validation is load-bearing; config already uses pydantic-settings |
| State store | SQLite via `sqlite3` + `asyncio.to_thread` | Two writes a day does not justify an `aiosqlite` dependency |
| Fixture format | JSONL of trade ticks | Exercises the aggregator, the component most likely to hide lookahead bugs |
| Position | Simulated `PositionTracker` | Needed for cutoff flatten and for `Context.position` |
| Sizing | Strategy sets absolute quantity | Per-account config carries enable/disable only, no multipliers |
| Cutoff | Emits a flatten signal | Owned by `SessionManager`, not the risk layer |
| Notify provider | Deferred | Abstract base plus dry-run implementation; ntfy or Pushover slots in later |
| Deployment | Local now, VPS later | All paths config-driven off `data_dir`; no hardcoded `/var/run` |

## Repo structure

```
nq-agent/
├── pyproject.toml
├── .env.example
├── .gitignore
├── config/
│   ├── base.yaml
│   ├── paper.yaml
│   └── live.yaml
├── docs/superpowers/specs/
├── src/nq_agent/
│   ├── __init__.py
│   ├── main.py              # entrypoint, wiring, CLI
│   ├── config.py            # pydantic-settings
│   ├── models.py            # core models
│   ├── clock.py             # Clock ABC, RealClock, SimClock
│   ├── session.py           # SessionManager: lifecycle + cutoff flatten
│   ├── position.py          # PositionTracker
│   ├── context.py           # Context passed to Strategy
│   ├── feed/
│   │   ├── base.py          # DataFeed ABC
│   │   ├── databento.py
│   │   ├── replay.py        # JSONL tick replay
│   │   └── aggregator.py    # tick -> bar, bar -> higher timeframe
│   ├── strategy/
│   │   ├── base.py          # Strategy ABC  <- the one interface
│   │   ├── stub.py          # never fires
│   │   └── always.py        # fires once, for pipeline testing
│   ├── risk/
│   │   ├── limits.py        # trade count, cutoff, kill switch, duplicate guard
│   │   └── accounts.py      # per-account enable/disable
│   ├── execution/
│   │   ├── base.py          # Executor ABC
│   │   ├── webhook.py       # Signal Trade App client
│   │   ├── notify.py        # NotifyExecutor ABC + DryRunNotifier
│   │   └── dryrun.py        # logs only
│   ├── router.py            # fan-out to executors
│   ├── journal.py           # structured JSONL log
│   └── state.py             # SQLite persistence + crash recovery
└── tests/
    ├── fixtures/            # recorded tick data
    └── ...
```

## Core models

Pydantic v2, `model_config = ConfigDict(frozen=True)`. These are the contracts every module speaks.

### Bar

```
symbol: str
timeframe: str            # "1m", "5m"
open_time: datetime       # UTC, tz-aware
open, high, low, close: Decimal
volume: int
closed: bool              # always True on emission; field exists for aggregator internals
```

`close_time` is a computed property: `open_time + timeframe_duration`.

### Signal

```
id: str                   # uuid4
timestamp: datetime       # UTC, from Context.clock, never wall clock
symbol: str
intent: SignalIntent      # ENTRY | FLATTEN
direction: Direction      # LONG | SHORT
entry_price: Decimal | None
stop_price: Decimal | None
target_price: Decimal | None
quantity: int
reason: str
metadata: dict
```

Validators, enforced at construction:

- `intent == ENTRY` requires `entry_price`, `stop_price`, `target_price` all present, and `quantity >= 1`
- `intent == ENTRY` and `direction == LONG` requires `stop_price < entry_price < target_price`
- `intent == ENTRY` and `direction == SHORT` requires `target_price < entry_price < stop_price`
- `intent == FLATTEN` requires all three price fields to be `None`

For `FLATTEN`, `direction` is the direction of the position being closed and `quantity` is the open size. The executor inverts it — flattening a LONG sends a sell. This keeps one signal type through the router, journal, and every executor.

Prices are absolute, never offsets. Converting to points, ticks, or dollars is the executor's job. This keeps strategy logic broker-agnostic.

### OrderResult

```
signal_id: str
executor_name: str
success: bool
account_id: str | None
latency_ms: int
error: str | None
raw_response: dict
```

### Position

```
symbol: str
direction: Direction
quantity: int
entry_price: Decimal
entry_time: datetime
stop_price: Decimal
target_price: Decimal
```

### SessionState

```
session_date: date        # New York calendar date
trades_taken: int
is_halted: bool
strategy_state: dict
last_bar_time: datetime | None
position: Position | None
```

### RiskVeto

```
signal_id: str
reason: VetoReason        # MAX_TRADES | PAST_CUTOFF | KILL_SWITCH
                          # | ACCOUNT_DISABLED | DUPLICATE_SIGNAL | SESSION_CLOSED
detail: str
```

## Interfaces

### `feed/base.py`

```python
class DataFeed(ABC):
    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]: ...

    def stream(
        self, symbol: str, timeframes: list[str]
    ) -> AsyncIterator[Bar]: ...

    async def close(self) -> None: ...
```

`stream` yields only closed bars. Never emit partial bars — every lookahead bug starts there. When a provider streams ticks, `aggregator.py` builds bars and holds them until close.

**Ordering guarantee.** Bars from all requested timeframes arrive on one stream, ordered by close time. When a 1m and a 5m close on the same boundary, the 1m is emitted first. A 5m arriving before its own final 1m is a lookahead bug, so this tie-break gets an explicit test.

**Gap handling.** No synthetic bars. A period with zero trades produces no bar. A 5m built from 1m bars uses whatever 1m bars fell inside its window. Strategies must tolerate gaps. Every gap is journaled as a `bar_gap` event.

### `strategy/base.py` — the single interface that later gets filled in

```python
class Strategy(ABC):
    name: str
    required_timeframes: list[str]

    def on_bar(self, bar: Bar, context: Context) -> Signal | None: ...
    def on_session_start(self, session_date: date) -> None: ...
    def on_session_end(self, session_date: date) -> None: ...
    def get_state(self) -> dict: ...
    def restore_state(self, state: dict) -> None: ...
```

Deliberately sync. Strategy does no I/O — no network, no file access, no `datetime.now()`. Making these plain `def` means a strategy structurally cannot await a network call. Purity is enforced by the type system rather than by discipline.

### `context.py`

Read-only view handed to the strategy on every bar.

```python
class Context(Protocol):
    def bars(self, timeframe: str, count: int) -> Sequence[Bar]: ...
    @property
    def position(self) -> Position | None: ...
    @property
    def trades_taken(self) -> int: ...
    @property
    def now(self) -> datetime: ...          # from Clock, never wall clock
    @property
    def session_date(self) -> date: ...
    @property
    def is_warmup(self) -> bool: ...        # True during crash-recovery backfill
```

Bar history is a bounded ring buffer per timeframe, size from config (`context.history_bars`, default 500).

### `execution/base.py`

```python
class Executor(ABC):
    name: str
    enabled: bool
    async def execute(self, signal: Signal) -> OrderResult: ...
    async def health_check(self) -> bool: ...
```

`NotifyExecutor` subclasses `Executor` and adds `async def alert(self, message: str) -> None` for router failure alerts. `DryRunNotifier` implements it by logging.

`health_check` is called by `main.py` once at startup for every enabled executor. A failure logs loudly and journals `executor_alert` but does not abort the run — a dead notification channel should not stop the webhook leg from trading.

## Clock and sessions

### `clock.py`

All time handling is isolated here. Nothing else in the codebase calls `datetime.now()`.

- Internal storage UTC, always, tz-aware
- Session windows defined in `America/New_York` via `zoneinfo`, converted at the boundary
- `Clock` ABC with `RealClock` and `SimClock`. `SimClock` is what makes replay tests deterministic — it advances only when fed a bar or tick timestamp, never on its own
- Helpers: `is_session_open()`, `is_before_cutoff()`, `session_date_for(ts)`

DST correctness gets its own test file with explicit dates: spring forward 2026-03-08, fall back 2026-11-01.

### `session.py`

`SessionManager` owns session lifecycle. It is the only component that generates signals other than the strategy.

Responsibilities:

- Detect session rollover from bar timestamps and call `on_session_start` / `on_session_end`
- At cutoff, if `PositionTracker` reports an open position, emit a `FLATTEN` signal

Flatten is signal generation, so it cannot live in the risk layer without breaking the veto-only invariant. `SessionManager` owns it instead. Risk stays pure veto.

### `position.py`

`PositionTracker` maintains simulated position state from signals and bars.

- Opens on a successful `ENTRY` signal, filled at `signal.entry_price`
- Closes when a bar's range touches stop or target
- When one bar touches both, **stop wins**. Pessimistic and unambiguous; no inside-bar guessing
- Closes on `FLATTEN`
- Emits `position_opened` and `position_closed` journal events

## Risk layer

Sits between strategy and router. Vetoes only. It can never modify a signal.

| Check | Applies to |
|---|---|
| Max trades per session | ENTRY only |
| Session cutoff time | ENTRY only |
| Kill switch file present | ENTRY only |
| Session closed | ENTRY only |
| Per-account enable/disable | ENTRY only |
| Duplicate signal guard | ENTRY only |

**`FLATTEN` always passes risk.** A kill switch that traps you in an open position is worse than no kill switch.

Details:

- Kill switch: file existence at `risk.kill_switch_path`, checked before every execution, not cached
- Duplicate guard: same symbol, same direction, same intent, within `risk.duplicate_window_seconds` (default 60) → reject
- Every veto is journaled with its `VetoReason` and detail string

**Account filtering is two-tier.** Per-account enable/disable is read from config at signal time, so an account can be disabled without a restart. The risk layer filters each executor's account list: an executor left with zero enabled accounts is skipped and journaled. Only when *no* executor retains any enabled account is the whole signal vetoed with `ACCOUNT_DISABLED`.

## Router

Fans one `Signal` out to N executors.

Sequence:

1. Notify executors run first and are awaited to completion or timeout. You are the slow leg on the manual account, so you get the head start.
2. Remaining executors then run concurrently via `asyncio.gather(..., return_exceptions=True)`, each wrapped in its own `asyncio.wait_for`.

`asyncio.TaskGroup` is deliberately not used — it cancels siblings on failure, and one executor failing must never block another.

Every `OrderResult` is journaled regardless of outcome. A timeout produces an `OrderResult` with `success=False` and `error="timeout"`, not an exception.

Partial-fan behavior is configurable, never hardcoded:

- `continue` — log the failure and proceed
- `alert_only` — additionally push a failure alert through the notify executors

Neither mode attempts to unwind legs that already succeeded.

## State and recovery

### `state.py`

SQLite at `{data_dir}/state.db`. `SessionState` is written after every state transition: session start, signal emitted, order result, position change, session end.

Startup sequence:

1. Load today's `SessionState` if it exists
2. Call `strategy.restore_state()` and restore `PositionTracker` from `SessionState.position`
3. Backfill bars from `last_bar_time` to now via `get_bars`
4. Replay them through the strategy in **warmup mode**
5. Resume the live stream

`PositionTracker` runs live during warmup, not suppressed. A stop hit during the downtime must be recognised on restart, otherwise the system believes it holds a position it no longer has.

**Warmup mode is the critical detail.** During backfill replay, `Context.is_warmup` is `True` and any signal the strategy returns is suppressed and journaled as `signal_suppressed_backfill`. Strategy internal state rebuilds correctly, but a signal whose moment passed three minutes ago never reaches an executor. Once backfill completes, warmup clears and live bars fire normally.

A restart at 10:15 must not lose the morning. This test gets written early — it is the one that catches sloppy state design.

## Journal

Structured JSON lines, one file per session date at `{data_dir}/journal/{session_date}.jsonl`.

Every record carries `ts` (UTC ISO-8601), `event`, and an event-specific payload. Event types:

`session_start`, `session_end`, `bar_gap`, `feed_error`, `signal_emitted`, `signal_suppressed_backfill`, `risk_veto`, `order_result`, `position_opened`, `position_closed`, `state_transition`, `executor_alert`.

Over-log rather than under-log. This is the debugging record, and it later feeds the LLM filter's shadow-mode evaluation.

Writes go through `asyncio.to_thread` to avoid blocking the event loop.

## Config

Pydantic-settings, layered: `base.yaml` → environment-specific yaml → env vars. Secrets only in env, never in yaml. `.env.example` lists every required key with dummy values.

The YAML layer is plain data with no variable interpolation. Any value that derives from another (`kill_switch_path` from `data_dir`, journal and state paths) is left null in YAML and resolved in `config.py` by a pydantic model validator.

```yaml
symbol: NQ
timeframes: [1m, 5m]
data_dir: ./var
session:
  timezone: America/New_York
  open: "09:30"
  cutoff: "16:30"
context:
  history_bars: 500
risk:
  max_trades_per_day: 2
  duplicate_window_seconds: 60
  kill_switch_path: null       # null resolves to {data_dir}/nq-agent.halt in code
router:
  partial_fan: continue        # continue | alert_only
  executor_timeout_seconds: 5
  notify_timeout_seconds: 3
executors:
  - name: signaltradeapp
    type: webhook
    enabled: true
    accounts: [tradeify, mff, fundednext]
  - name: manual_lucid
    type: notify
    enabled: true
```

## Error handling

| Failure | Behavior |
|---|---|
| Feed disconnect | Exponential backoff reconnect, journal `feed_error`, on reconnect run the same backfill-with-warmup path as startup |
| Executor timeout | `OrderResult(success=False, error="timeout")`, journaled, other executors unaffected |
| Executor exception | Caught by `gather(return_exceptions=True)`, converted to a failed `OrderResult`, never propagates |
| Strategy raises | Caught, journaled, strategy marked halted for the session, no further `on_bar` calls |
| State write fails | Logged loudly and re-raised — silent state corruption is worse than a crash |
| Malformed bar or tick | Skipped, journaled, counter incremented |

## Testing

Replay tests are the backbone. `ReplayFeed` reads a JSONL tick fixture and pushes bars through the whole pipeline with `SimClock` and `DryRunExecutor`. Deterministic, no network, fast.

- Fixtures: record 5–10 real trading days once, commit them
- `DryRunExecutor` produces the same `OrderResult` shape as the real one, so tests exercise the real code path

Unit test coverage, each its own file:

- Clock and DST — spring forward 2026-03-08, fall back 2026-11-01
- Aggregator — tick to 1m, 1m to 5m, same-boundary ordering, gap handling
- Signal validators — every price-ordering rule, both intents
- Risk limits — every `VetoReason`, plus `FLATTEN` bypassing all of them
- Router — concurrency, per-executor timeout isolation, both partial-fan modes, notify-first ordering
- State recovery — mid-session kill and restart, warmup suppression
- `PositionTracker` — stop-wins-on-ambiguous-bar

Tooling: `pytest`, `pytest-asyncio`, `ruff`, `mypy --strict` on `src/`.

## Build sequence

Each step ends with tests green before moving to the next.

1. Project skeleton, `pyproject.toml`, config loading, `.env.example`, `.gitignore`
2. `models.py` with full validator coverage, `clock.py` with full DST coverage
3. `journal.py` — every later component writes to it, so it comes before them
4. `DataFeed` ABC, `ReplayFeed`, aggregator, tested against fixtures
5. `Strategy` ABC, `Context`, `StubStrategy`, `AlwaysStrategy`
6. `Executor` ABC, `DryRunExecutor`, `DryRunNotifier`, then `Router` with concurrency and partial-fan handling
7. Risk layer and kill switch
8. `position.py` and `session.py`, including cutoff flatten
9. `state.py` and the crash-recovery test
10. `main.py` wiring — end-to-end replay run with `AlwaysStrategy` into `DryRunExecutor`
11. Real `DatabentoFeed` against the same ABC
12. Real `WebhookExecutor` and a concrete `NotifyExecutor`, tested against the vendor's paper endpoint

Steps 1–10 need no external accounts, so the entire system gets built before spending a dollar.

## Definition of done

```
python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl
```

runs a full simulated session, emits a signal from `AlwaysStrategy`, routes it to a dry-run executor and a dry-run notifier, writes a complete journal, survives a mid-session kill and restart with state intact — and the strategy module is a 20-line stub.

At that point, adding the real strategy is implementing one class.
