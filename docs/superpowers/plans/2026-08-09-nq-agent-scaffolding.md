# NQ Agent Scaffolding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build every layer around the strategy so that adding real rules later means implementing one interface and nothing else.

**Architecture:** An async engine reads closed bars from a `DataFeed`, hands each to a sync `Strategy` through a read-only `Context`, passes any returned `Signal` through a veto-only risk layer, and fans it out to N concurrent executors. A `SessionManager` owns session lifecycle and is the only other component allowed to generate signals — it emits a `FLATTEN` at cutoff. All state persists to SQLite after every transition so a mid-session restart replays the morning in warmup mode without re-firing stale signals.

**Tech Stack:** Python 3.12, uv, pydantic v2, pydantic-settings, pytest, pytest-asyncio, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-09-nq-agent-scaffolding-design.md`

## Global Constraints

- `requires-python = ">=3.12"`. Package manager is `uv`.
- **Write 3.10-compatible spellings even though the floor is 3.12.** The CI sandbox this is developed in only has Python 3.10, so the test suite must run there. Three rules, all upward-compatible to 3.13 and beyond — do not "modernise" them back:
  - `class Direction(str, Enum)`, never `StrEnum`
  - `timezone.utc`, never `datetime.UTC`
  - `except asyncio.TimeoutError`, never bare `except TimeoutError` (in 3.11+ the two are the same object, so catching the `asyncio` name is correct on every version)
- Run tests with `python3 -m pytest`, not `uv run pytest` — the sandbox venv is the system interpreter with dev deps installed.
- All datetimes are timezone-aware UTC internally. Session windows are defined in `America/New_York` via `zoneinfo` and converted at the boundary.
- Nothing outside `clock.py` calls `datetime.now()`. Everything else reads time from a `Clock`.
- `Strategy` methods are sync `def`. Strategy does no network, no file access, no wall-clock reads.
- All prices are `Decimal`, never `float`.
- Models are pydantic v2 with `model_config = ConfigDict(frozen=True)`.
- The risk layer vetoes only. It never modifies a signal.
- `FLATTEN` signals bypass every risk check.
- No synthetic bars. A period with zero trades produces no bar.
- Bars are emitted only when closed. Never emit a partial bar.
- `ruff check` and `mypy --strict src/` must pass before every commit.
- Commit after every task using the message given in that task's final step.

## Amendments during execution

Recorded as they happen, so the plan text and the branch stay honest.

- **Task 1.** Ruff's `target-version` was lowered from `py312` to `py310`. At `py312` the `UP` rule set rejects all three mandated 3.10-compatible spellings (`UP042` on `class X(str, Enum)`, `UP017` on `timezone.utc`, `UP041` on `except asyncio.TimeoutError`), which would have failed the pre-commit lint gate from Task 2 onward. `requires-python` is unchanged at `>=3.12`; the two settings are independent.
- **Task 1.** `SessionConfig`, `ContextConfig`, `RouterConfig`, `ExecutorConfig` and `Settings` gained `frozen=True`, which the task's original code block omitted. `RiskConfig` remains the single mutable model so `resolve_derived_paths` can assign `kill_switch_path`.
- **Task 2.** The task's `tests/test_models.py` imported `Tick` but asserted nothing against it, so `ruff` flagged the import as unused (`F401`). Rather than silence it with a `noqa`, two real tests were added covering `Tick`'s UTC normalisation and frozen-ness, matching the coverage every other model in the file gets.
- **Task 2.** `SessionState.last_bar_time` was missing the UTC validator its five sibling datetime fields had, so it silently accepted naive datetimes. Fixed structurally: `models.py` now declares `UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]` once and all six datetime fields use it, replacing five hand-written validators. A new datetime field is now UTC-enforced by its type rather than by remembering to add a validator.
- **Task 3.** The brief's `test_session_date_is_stable_across_the_fall_back_repeated_hour` had two `assert` lines at 103 characters, over the repo's `ruff` `line-length = 100` (`E501`), which blocks the `ruff check . && ... && git commit` chain the task's own Step 5 requires. Wrapped by extracting `edt_instant`/`est_instant` locals named after the test's own comment; the datetime literals, the calendar date, and the assertions are byte-identical to the brief otherwise. `src/nq_agent/clock.py` needed no changes and matches the brief's Step 3 code verbatim.
- **Task 3.** Added `tzdata>=2024.1` to dependencies. `zoneinfo` has no bundled IANA database and falls back to the host's; Windows ships none, so `SessionCalendar` would raise `ZoneInfoNotFoundError` on the development machine. Also set `asyncio_default_fixture_loop_scope = "function"` to silence a `pytest_asyncio` deprecation warning ahead of Task 4's async tests, and added the `SimClock` naive-datetime tests the task's own test list omitted.

## Scope

This plan covers steps 1–10 of the spec's build sequence — everything reachable without an external account. `DatabentoFeed` and the real `WebhookExecutor`/`NotifyExecutor` (spec steps 11–12) need vendor credentials and API docs, so they get their own plan once those exist. Completing this plan satisfies the spec's definition of done.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Deps, tool config for ruff/mypy/pytest |
| `config/base.yaml` | Shared defaults |
| `config/paper.yaml`, `config/live.yaml` | Environment overlays |
| `src/nq_agent/config.py` | Settings models, YAML layering, path resolution |
| `src/nq_agent/models.py` | Every cross-module contract: enums, `Tick`, `Bar`, `Signal`, `OrderResult`, `Position`, `PositionClose`, `SessionState`, `RiskVeto` |
| `src/nq_agent/clock.py` | `Clock` ABC, `RealClock`, `SimClock`, `SessionCalendar` |
| `src/nq_agent/journal.py` | Append-only JSONL event log |
| `src/nq_agent/feed/aggregator.py` | Ticks into closed bars across timeframes |
| `src/nq_agent/feed/base.py` | `DataFeed` ABC |
| `src/nq_agent/feed/replay.py` | JSONL tick fixture playback |
| `src/nq_agent/context.py` | Read-only view handed to the strategy |
| `src/nq_agent/strategy/base.py` | `Strategy` ABC — the one interface |
| `src/nq_agent/strategy/stub.py` | Never fires |
| `src/nq_agent/strategy/always.py` | Fires once per session |
| `src/nq_agent/execution/base.py` | `Executor` and `NotifyExecutor` ABCs |
| `src/nq_agent/execution/dryrun.py` | `DryRunExecutor`, `DryRunNotifier` |
| `src/nq_agent/router.py` | Notify-first, then concurrent fan-out |
| `src/nq_agent/risk/accounts.py` | Per-account enable/disable, re-read at signal time |
| `src/nq_agent/risk/limits.py` | `RiskManager` — every veto |
| `src/nq_agent/position.py` | `PositionTracker` |
| `src/nq_agent/session.py` | `SessionManager` — lifecycle and cutoff flatten |
| `src/nq_agent/state.py` | SQLite `SessionState` persistence |
| `src/nq_agent/main.py` | CLI, wiring, engine loop |

**One deliberate implementation choice.** The spec describes 5m bars as a rollup of 1m bars. The aggregator instead builds every timeframe directly from ticks. The observable contract is identical when no 1m bars are missing, and strictly more correct when they are — a 5m bar still forms even if a constituent minute had zero trades. Ordering and gap rules are unchanged.

**One clarification on accounts.** Executor instances are per-account. A config entry with `accounts: [tradeify, mff, fundednext]` expands at wiring time into three executor instances, each carrying one `account_id`. This makes the spec's two-tier account filtering fall out for free: a disabled account is a disabled instance, and `ACCOUNT_DISABLED` fires only when zero instances remain enabled.

Live enable/disable lives in `{data_dir}/accounts.yaml`, a flat `name: bool` mapping that the router re-reads on every signal. The file is optional — absent means no overrides. It is deliberately not part of the layered YAML config, because that config is read once at startup and this file must be editable mid-session:

```yaml
tradeify: true
mff: true
fundednext: false
```

---

### Task 1: Project skeleton and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `config/base.yaml`, `config/paper.yaml`, `config/live.yaml`
- Create: `src/nq_agent/__init__.py`
- Create: `src/nq_agent/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Settings` with fields `symbol: str`, `timeframes: list[str]`, `data_dir: Path`, `session: SessionConfig`, `context: ContextConfig`, `risk: RiskConfig`, `router: RouterConfig`, `executors: list[ExecutorConfig]`, `databento_api_key: str | None`; properties `journal_dir: Path`, `state_db_path: Path`. Loader `load_settings(config_path: Path) -> Settings`.

- [ ] **Step 1: Create the project skeleton**

```bash
mkdir -p src/nq_agent/feed src/nq_agent/strategy src/nq_agent/execution src/nq_agent/risk
mkdir -p tests/fixtures config docs/superpowers/plans
touch src/nq_agent/__init__.py src/nq_agent/feed/__init__.py src/nq_agent/strategy/__init__.py
touch src/nq_agent/execution/__init__.py src/nq_agent/risk/__init__.py tests/__init__.py
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "nq-agent"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "ruff>=0.5",
    "mypy>=1.10",
    "types-PyYAML",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/nq_agent"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py310"  # deliberately below the 3.12 floor; see Global Constraints

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src/nq_agent"]
```

- [ ] **Step 3: Install the environment**

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Expected: installs without error and reports the resolved package set.

- [ ] **Step 4: Write the config YAML files**

`config/base.yaml`:

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
  kill_switch_path: null
router:
  partial_fan: continue
  executor_timeout_seconds: 5.0
  notify_timeout_seconds: 3.0
executors: []
```

`config/paper.yaml`:

```yaml
executors:
  - name: dryrun_broker
    type: dryrun
    enabled: true
    accounts: [tradeify, mff, fundednext]
  - name: dryrun_notify
    type: notify
    enabled: true
    accounts: []
```

`config/live.yaml`:

```yaml
executors:
  - name: signaltradeapp
    type: webhook
    enabled: true
    accounts: [tradeify, mff, fundednext]
  - name: manual_lucid
    type: notify
    enabled: true
    accounts: []
```

- [ ] **Step 5: Write `.env.example`**

```
# Secrets live here only, never in YAML.
NQ_DATABENTO_API_KEY=db-dummy-key-replace-me
NQ_WEBHOOK_URL=https://example.invalid/webhook
NQ_WEBHOOK_TOKEN=dummy-token-replace-me
NQ_NOTIFY_TOPIC=nq-agent-dummy-topic
```

- [ ] **Step 6: Write the failing test**

`tests/test_config.py`:

```python
from datetime import time
from pathlib import Path

from nq_agent.config import load_settings


def test_base_config_loads_defaults(tmp_path: Path) -> None:
    settings = load_settings(Path("config/base.yaml"))
    assert settings.symbol == "NQ"
    assert settings.timeframes == ["1m", "5m"]
    assert settings.session.cutoff == time(16, 30)
    assert settings.risk.max_trades_per_day == 2
    assert settings.router.partial_fan == "continue"


def test_environment_overlay_merges_over_base() -> None:
    settings = load_settings(Path("config/paper.yaml"))
    assert settings.symbol == "NQ"
    assert len(settings.executors) == 2
    assert settings.executors[0].type == "dryrun"
    assert settings.executors[0].accounts == ["tradeify", "mff", "fundednext"]


def test_null_kill_switch_path_resolves_under_data_dir() -> None:
    settings = load_settings(Path("config/base.yaml"))
    assert settings.risk.kill_switch_path == Path("var/nq-agent.halt")


def test_derived_paths_hang_off_data_dir() -> None:
    settings = load_settings(Path("config/base.yaml"))
    assert settings.journal_dir == Path("var/journal")
    assert settings.state_db_path == Path("var/state.db")
```

- [ ] **Step 7: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.config'`

- [ ] **Step 8: Write `config.py`**

```python
from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_CONFIG = Path("config/base.yaml")


class SessionConfig(BaseModel):
    timezone: str = "America/New_York"
    open: time = time(9, 30)
    cutoff: time = time(16, 30)


class ContextConfig(BaseModel):
    history_bars: int = 500


class RiskConfig(BaseModel):
    max_trades_per_day: int = 2
    duplicate_window_seconds: int = 60
    kill_switch_path: Path | None = None


class RouterConfig(BaseModel):
    partial_fan: Literal["continue", "alert_only"] = "continue"
    executor_timeout_seconds: float = 5.0
    notify_timeout_seconds: float = 3.0


class ExecutorConfig(BaseModel):
    name: str
    type: Literal["webhook", "notify", "dryrun"]
    enabled: bool = True
    accounts: list[str] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NQ_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    symbol: str = "NQ"
    timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m"])
    data_dir: Path = Path("./var")
    session: SessionConfig = SessionConfig()
    context: ContextConfig = ContextConfig()
    risk: RiskConfig = RiskConfig()
    router: RouterConfig = RouterConfig()
    executors: list[ExecutorConfig] = Field(default_factory=list)

    databento_api_key: str | None = None
    webhook_url: str | None = None
    webhook_token: str | None = None
    notify_topic: str | None = None

    @model_validator(mode="after")
    def resolve_derived_paths(self) -> Settings:
        if self.risk.kill_switch_path is None:
            self.risk.kill_switch_path = self.data_dir / "nq-agent.halt"
        return self

    @property
    def journal_dir(self) -> Path:
        return self.data_dir / "journal"

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "state.db"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    loaded = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config file must contain a mapping: {path}")
    return loaded


def load_settings(config_path: Path) -> Settings:
    """Layer base.yaml under the given config, then let env vars supply secrets.

    YAML carries structure. Environment carries secrets. They never overlap,
    so there is no precedence conflict between the two layers.
    """
    data = _read_yaml(BASE_CONFIG)
    if config_path.resolve() != BASE_CONFIG.resolve():
        data = _deep_merge(data, _read_yaml(config_path))
    return Settings(**data)
```

`Settings` mutates `risk.kill_switch_path` inside the validator, so `RiskConfig` stays non-frozen. This is the one config model that is intentionally mutable; every model in `models.py` is frozen.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 4 passed

- [ ] **Step 10: Run the linters**

Run: `uv run ruff check . && uv run mypy src/nq_agent`
Expected: both clean

- [ ] **Step 11: Commit**

```bash
git add pyproject.toml .env.example config/ src/ tests/
git commit -m "feat: project skeleton and layered configuration"
```

---

### Task 2: Core models

**Files:**
- Create: `src/nq_agent/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Direction`, `SignalIntent`, `VetoReason` enums; `TIMEFRAME_SECONDS: dict[str, int]`; `Tick`, `Bar`, `Signal`, `OrderResult`, `Position`, `PositionClose`, `SessionState`, `RiskVeto` models. `Bar.close_time` property. `Signal.id` auto-generated uuid4 hex string.

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nq_agent.models import (
    Bar,
    Direction,
    OrderResult,
    Position,
    PositionClose,
    RiskVeto,
    SessionState,
    Signal,
    SignalIntent,
    Tick,
    VetoReason,
)


def _bar(**overrides: object) -> Bar:
    defaults: dict[str, object] = {
        "symbol": "NQ",
        "timeframe": "1m",
        "open_time": datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc),
        "open": Decimal("20100"),
        "high": Decimal("20110"),
        "low": Decimal("20090"),
        "close": Decimal("20105"),
        "volume": 500,
    }
    defaults.update(overrides)
    return Bar(**defaults)  # type: ignore[arg-type]


def test_bar_close_time_derives_from_timeframe() -> None:
    bar = _bar()
    assert bar.close_time == datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc)
    assert _bar(timeframe="5m").close_time == datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc)


def test_bar_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _bar(open_time=datetime(2026, 7, 15, 13, 30))


def test_bar_rejects_unknown_timeframe() -> None:
    with pytest.raises(ValidationError, match="unsupported timeframe"):
        _bar(timeframe="3m")


def test_bar_is_frozen() -> None:
    bar = _bar()
    with pytest.raises(ValidationError):
        bar.close = Decimal("1")  # type: ignore[misc]


def _entry(**overrides: object) -> Signal:
    defaults: dict[str, object] = {
        "timestamp": datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        "symbol": "NQ",
        "intent": SignalIntent.ENTRY,
        "direction": Direction.LONG,
        "entry_price": Decimal("20105"),
        "stop_price": Decimal("20095"),
        "target_price": Decimal("20125"),
        "quantity": 1,
        "reason": "test",
    }
    defaults.update(overrides)
    return Signal(**defaults)  # type: ignore[arg-type]


def test_entry_signal_gets_a_unique_id() -> None:
    assert _entry().id != _entry().id


def test_long_entry_requires_stop_below_entry_below_target() -> None:
    with pytest.raises(ValidationError, match="stop_price < entry_price < target_price"):
        _entry(stop_price=Decimal("20115"))


def test_short_entry_requires_target_below_entry_below_stop() -> None:
    signal = _entry(
        direction=Direction.SHORT,
        stop_price=Decimal("20115"),
        target_price=Decimal("20085"),
    )
    assert signal.direction is Direction.SHORT
    with pytest.raises(ValidationError, match="target_price < entry_price < stop_price"):
        _entry(direction=Direction.SHORT)


def test_entry_requires_all_three_prices() -> None:
    with pytest.raises(ValidationError, match="requires entry_price, stop_price, target_price"):
        _entry(target_price=None)


def test_entry_requires_positive_quantity() -> None:
    with pytest.raises(ValidationError):
        _entry(quantity=0)


def test_flatten_forbids_price_fields() -> None:
    with pytest.raises(ValidationError, match="must not carry price fields"):
        _entry(intent=SignalIntent.FLATTEN)


def test_flatten_signal_is_valid_without_prices() -> None:
    signal = Signal(
        timestamp=datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc),
        symbol="NQ",
        intent=SignalIntent.FLATTEN,
        direction=Direction.LONG,
        quantity=1,
        reason="session cutoff",
    )
    assert signal.entry_price is None


def test_order_result_and_veto_round_trip() -> None:
    result = OrderResult(
        signal_id="abc",
        executor_name="dryrun:tradeify",
        success=True,
        account_id="tradeify",
        latency_ms=12,
    )
    assert result.error is None
    veto = RiskVeto(signal_id="abc", reason=VetoReason.KILL_SWITCH, detail="halt file present")
    assert veto.reason is VetoReason.KILL_SWITCH


def test_position_close_carries_exit_detail() -> None:
    position = Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        entry_price=Decimal("20105"),
        entry_time=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        stop_price=Decimal("20095"),
        target_price=Decimal("20125"),
    )
    closed = PositionClose(
        position=position,
        exit_price=Decimal("20095"),
        exit_time=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
        exit_reason="STOP",
    )
    assert closed.exit_reason == "STOP"


def test_session_state_defaults_are_empty() -> None:
    state = SessionState(session_date=date(2026, 7, 15))
    assert state.trades_taken == 0
    assert state.is_halted is False
    assert state.strategy_state == {}
    assert state.last_bar_time is None
    assert state.position is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.models'`

- [ ] **Step 3: Write `models.py`**

```python
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalIntent(str, Enum):
    ENTRY = "ENTRY"
    FLATTEN = "FLATTEN"


class VetoReason(str, Enum):
    MAX_TRADES = "MAX_TRADES"
    PAST_CUTOFF = "PAST_CUTOFF"
    KILL_SWITCH = "KILL_SWITCH"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    SESSION_CLOSED = "SESSION_CLOSED"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Tick(Frozen):
    symbol: str
    ts: datetime
    price: Decimal
    size: int

    @field_validator("ts")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class Bar(Frozen):
    symbol: str
    timeframe: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    closed: bool = True

    @field_validator("open_time")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("timeframe")
    @classmethod
    def _known_timeframe(cls, value: str) -> str:
        if value not in TIMEFRAME_SECONDS:
            raise ValueError(f"unsupported timeframe: {value}")
        return value

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(seconds=TIMEFRAME_SECONDS[self.timeframe])


class Signal(Frozen):
    id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: datetime
    symbol: str
    intent: SignalIntent = SignalIntent.ENTRY
    direction: Direction
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    quantity: int = Field(ge=1)
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _check_intent_prices(self) -> Signal:
        prices = (self.entry_price, self.stop_price, self.target_price)
        if self.intent is SignalIntent.FLATTEN:
            if any(p is not None for p in prices):
                raise ValueError("FLATTEN must not carry price fields")
            return self

        if any(p is None for p in prices):
            raise ValueError("ENTRY requires entry_price, stop_price, target_price")

        entry, stop, target = self.entry_price, self.stop_price, self.target_price
        assert entry is not None and stop is not None and target is not None
        if self.direction is Direction.LONG and not (stop < entry < target):
            raise ValueError("LONG ENTRY requires stop_price < entry_price < target_price")
        if self.direction is Direction.SHORT and not (target < entry < stop):
            raise ValueError("SHORT ENTRY requires target_price < entry_price < stop_price")
        return self


class OrderResult(Frozen):
    signal_id: str
    executor_name: str
    success: bool
    account_id: str | None = None
    latency_ms: int = 0
    error: str | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)


class Position(Frozen):
    symbol: str
    direction: Direction
    quantity: int
    entry_price: Decimal
    entry_time: datetime
    stop_price: Decimal
    target_price: Decimal

    @field_validator("entry_time")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class PositionClose(Frozen):
    position: Position
    exit_price: Decimal
    exit_time: datetime
    exit_reason: str

    @field_validator("exit_time")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class SessionState(Frozen):
    session_date: date
    trades_taken: int = 0
    is_halted: bool = False
    strategy_state: dict[str, Any] = Field(default_factory=dict)
    last_bar_time: datetime | None = None
    position: Position | None = None


class RiskVeto(Frozen):
    signal_id: str
    reason: VetoReason
    detail: str
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: 14 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/models.py tests/test_models.py
git commit -m "feat: core models with intent and price-ordering validators"
```

---

### Task 3: Clock, session calendar, and DST correctness

**Files:**
- Create: `src/nq_agent/clock.py`
- Test: `tests/test_clock.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Clock` ABC with `now() -> datetime`; `RealClock()`; `SimClock(start: datetime)` with `advance_to(ts: datetime) -> None`; `SessionCalendar(timezone: str, open_time: time, cutoff_time: time)` with `session_date_for(ts) -> date`, `is_session_open(ts) -> bool`, `is_before_cutoff(ts) -> bool`, `cutoff_utc(session_date) -> datetime`.

- [ ] **Step 1: Write the failing test**

`tests/test_clock.py`:

```python
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from nq_agent.clock import RealClock, SessionCalendar, SimClock

NY = ZoneInfo("America/New_York")


def calendar() -> SessionCalendar:
    return SessionCalendar("America/New_York", time(9, 30), time(16, 30))


def test_real_clock_returns_aware_utc() -> None:
    now = RealClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_sim_clock_only_moves_when_advanced() -> None:
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    clock = SimClock(start)
    assert clock.now() == start
    assert clock.now() == start
    clock.advance_to(datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc))
    assert clock.now() == datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc)


def test_sim_clock_refuses_to_go_backwards() -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))


def test_session_date_uses_new_york_calendar_date() -> None:
    # 01:00 UTC on the 16th is 21:00 on the 15th in New York.
    ts = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)
    assert calendar().session_date_for(ts) == date(2026, 7, 15)


def test_session_open_and_cutoff_during_edt() -> None:
    cal = calendar()
    # July is EDT, UTC-4. 09:30 ET == 13:30 UTC.
    assert cal.is_session_open(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)) is True
    assert cal.is_session_open(datetime(2026, 7, 15, 13, 29, tzinfo=timezone.utc)) is False
    assert cal.is_before_cutoff(datetime(2026, 7, 15, 20, 29, tzinfo=timezone.utc)) is True
    assert cal.is_before_cutoff(datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)) is False


def test_session_open_and_cutoff_during_est() -> None:
    cal = calendar()
    # January is EST, UTC-5. 09:30 ET == 14:30 UTC.
    assert cal.is_session_open(datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)) is True
    assert cal.is_session_open(datetime(2026, 1, 15, 14, 29, tzinfo=timezone.utc)) is False
    assert cal.is_before_cutoff(datetime(2026, 1, 15, 21, 29, tzinfo=timezone.utc)) is True
    assert cal.is_before_cutoff(datetime(2026, 1, 15, 21, 30, tzinfo=timezone.utc)) is False


def test_cutoff_utc_shifts_across_spring_forward() -> None:
    cal = calendar()
    # DST begins 2026-03-08. The day before is EST, the day after is EDT.
    assert cal.cutoff_utc(date(2026, 3, 7)) == datetime(2026, 3, 7, 21, 30, tzinfo=timezone.utc)
    assert cal.cutoff_utc(date(2026, 3, 9)) == datetime(2026, 3, 9, 20, 30, tzinfo=timezone.utc)


def test_cutoff_utc_shifts_across_fall_back() -> None:
    cal = calendar()
    # DST ends 2026-11-01. The day before is EDT, the day after is EST.
    assert cal.cutoff_utc(date(2026, 10, 31)) == datetime(2026, 10, 31, 20, 30, tzinfo=timezone.utc)
    assert cal.cutoff_utc(date(2026, 11, 2)) == datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)


def test_spring_forward_day_itself_is_edt_by_session_open() -> None:
    cal = calendar()
    # The 02:00 -> 03:00 jump happens before 09:30, so 2026-03-08 trades on EDT.
    assert cal.cutoff_utc(date(2026, 3, 8)) == datetime(2026, 3, 8, 20, 30, tzinfo=timezone.utc)


def test_fall_back_day_itself_is_est_by_session_open() -> None:
    cal = calendar()
    # The 02:00 -> 01:00 repeat happens before 09:30, so 2026-11-01 trades on EST.
    assert cal.cutoff_utc(date(2026, 11, 1)) == datetime(2026, 11, 1, 21, 30, tzinfo=timezone.utc)


def test_session_date_is_stable_across_the_fall_back_repeated_hour() -> None:
    cal = calendar()
    # 05:30 UTC on 2026-11-01 is 01:30 EDT; 06:30 UTC is 01:30 EST. Same NY date.
    assert cal.session_date_for(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)) == date(2026, 11, 1)
    assert cal.session_date_for(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)) == date(2026, 11, 1)


def test_naive_input_is_rejected() -> None:
    cal = calendar()
    with pytest.raises(ValueError, match="timezone-aware"):
        cal.session_date_for(datetime(2026, 7, 15, 13, 30))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_clock.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.clock'`

- [ ] **Step 3: Write `clock.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


def _require_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return ts


class Clock(ABC):
    """The only source of time in the system."""

    @abstractmethod
    def now(self) -> datetime:
        """Current time as timezone-aware UTC."""


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SimClock(Clock):
    """Deterministic clock. Advances only when fed a timestamp."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start).astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, ts: datetime) -> None:
        target = _require_aware(ts).astimezone(timezone.utc)
        if target < self._now:
            raise ValueError(f"clock cannot run backwards: {target} < {self._now}")
        self._now = target


class SessionCalendar:
    """Session windows in local exchange time, exposed as UTC comparisons."""

    def __init__(self, timezone: str, open_time: time, cutoff_time: time) -> None:
        self._zone = ZoneInfo(timezone)
        self._open = open_time
        self._cutoff = cutoff_time

    def session_date_for(self, ts: datetime) -> date:
        return _require_aware(ts).astimezone(self._zone).date()

    def _local_to_utc(self, session_date: date, at: time) -> datetime:
        local = datetime.combine(session_date, at, tzinfo=self._zone)
        return local.astimezone(timezone.utc)

    def open_utc(self, session_date: date) -> datetime:
        return self._local_to_utc(session_date, self._open)

    def cutoff_utc(self, session_date: date) -> datetime:
        return self._local_to_utc(session_date, self._cutoff)

    def is_session_open(self, ts: datetime) -> bool:
        ts = _require_aware(ts).astimezone(timezone.utc)
        session_date = self.session_date_for(ts)
        return self.open_utc(session_date) <= ts < self.cutoff_utc(session_date)

    def is_before_cutoff(self, ts: datetime) -> bool:
        ts = _require_aware(ts).astimezone(timezone.utc)
        return ts < self.cutoff_utc(self.session_date_for(ts))
```

Combining a `date` with a `time` under a `ZoneInfo` resolves the offset for that specific calendar day, which is what makes `cutoff_utc` land on 20:30 UTC in July and 21:30 UTC in January without any manual DST arithmetic.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_clock.py -v`
Expected: 12 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/clock.py tests/test_clock.py
git commit -m "feat: clock abstraction and DST-correct session calendar"
```

---

### Task 4: Journal

**Files:**
- Create: `src/nq_agent/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: `Clock` from `nq_agent.clock`
- Produces: `Journal(journal_dir: Path, clock: Clock)` with `async write(event: str, session_date: date, **payload: Any) -> None` and `path_for(session_date: date) -> Path`. Records are JSON objects with keys `ts`, `event`, plus the payload.

- [ ] **Step 1: Write the failing test**

`tests/test_journal.py`:

```python
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SimClock
from nq_agent.journal import Journal


async def test_write_creates_one_file_per_session_date(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    journal = Journal(tmp_path, clock)

    await journal.write("session_start", date(2026, 7, 15), symbol="NQ")
    await journal.write("session_start", date(2026, 7, 16), symbol="NQ")

    assert (tmp_path / "2026-07-15.jsonl").exists()
    assert (tmp_path / "2026-07-16.jsonl").exists()


async def test_records_carry_timestamp_and_event(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    journal = Journal(tmp_path, clock)

    await journal.write("risk_veto", date(2026, 7, 15), reason="KILL_SWITCH")

    line = (tmp_path / "2026-07-15.jsonl").read_text().strip()
    record = json.loads(line)
    assert record["ts"] == "2026-07-15T13:30:00+00:00"
    assert record["event"] == "risk_veto"
    assert record["reason"] == "KILL_SWITCH"


async def test_appends_rather_than_overwrites(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    journal = Journal(tmp_path, clock)

    await journal.write("a", date(2026, 7, 15))
    await journal.write("b", date(2026, 7, 15))

    lines = (tmp_path / "2026-07-15.jsonl").read_text().strip().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["a", "b"]


async def test_decimal_and_datetime_payloads_serialise(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    journal = Journal(tmp_path, clock)

    await journal.write(
        "signal_emitted",
        date(2026, 7, 15),
        entry_price=Decimal("20105.25"),
        at=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
    )

    record = json.loads((tmp_path / "2026-07-15.jsonl").read_text().strip())
    assert record["entry_price"] == "20105.25"
    assert record["at"] == "2026-07-15T13:31:00+00:00"


async def test_creates_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "journal"
    journal = Journal(target, SimClock(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)))
    await journal.write("session_start", date(2026, 7, 15))
    assert (target / "2026-07-15.jsonl").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_journal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.journal'`

- [ ] **Step 3: Write `journal.py`**

```python
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nq_agent.clock import Clock


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__} for the journal")


class Journal:
    """Append-only JSONL event log, one file per session date.

    Over-log rather than under-log. This is the debugging record and it later
    feeds the LLM filter's shadow-mode evaluation.
    """

    def __init__(self, journal_dir: Path, clock: Clock) -> None:
        self._dir = journal_dir
        self._clock = clock

    def path_for(self, session_date: date) -> Path:
        return self._dir / f"{session_date.isoformat()}.jsonl"

    def _append(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def write(self, event: str, session_date: date, **payload: Any) -> None:
        record: dict[str, Any] = {"ts": self._clock.now().isoformat(), "event": event}
        record.update(payload)
        line = json.dumps(record, default=_encode)
        await asyncio.to_thread(self._append, self.path_for(session_date), line)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_journal.py -v`
Expected: 5 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/journal.py tests/test_journal.py
git commit -m "feat: append-only JSONL journal keyed by session date"
```

---

### Task 5: Bar aggregator

**Files:**
- Create: `src/nq_agent/feed/aggregator.py`
- Test: `tests/test_aggregator.py`

**Interfaces:**
- Consumes: `Bar`, `Tick`, `TIMEFRAME_SECONDS` from `nq_agent.models`
- Produces: `BarAggregator(symbol: str, timeframes: list[str])` with `add_tick(tick: Tick) -> list[Bar]` and `flush() -> list[Bar]`. Both return closed bars ordered by `(close_time, timeframe duration)`.

- [ ] **Step 1: Write the failing test**

`tests/test_aggregator.py`:

```python
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nq_agent.feed.aggregator import BarAggregator
from nq_agent.models import Tick

START = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)


def tick(offset_seconds: int, price: str, size: int = 1) -> Tick:
    return Tick(
        symbol="NQ",
        ts=START + timedelta(seconds=offset_seconds),
        price=Decimal(price),
        size=size,
    )


def test_no_bar_emitted_until_the_bucket_closes() -> None:
    agg = BarAggregator("NQ", ["1m"])
    assert agg.add_tick(tick(0, "20100")) == []
    assert agg.add_tick(tick(30, "20110")) == []
    assert agg.add_tick(tick(59, "20105")) == []


def test_bar_ohlcv_is_correct_on_close() -> None:
    agg = BarAggregator("NQ", ["1m"])
    agg.add_tick(tick(0, "20100", 2))
    agg.add_tick(tick(20, "20120", 3))
    agg.add_tick(tick(40, "20090", 1))
    agg.add_tick(tick(59, "20105", 4))

    bars = agg.add_tick(tick(60, "20106"))
    assert len(bars) == 1
    bar = bars[0]
    assert bar.timeframe == "1m"
    assert bar.open_time == START
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("20100"),
        Decimal("20120"),
        Decimal("20090"),
        Decimal("20105"),
    )
    assert bar.volume == 10
    assert bar.closed is True


def test_one_minute_emitted_before_five_minute_on_shared_boundary() -> None:
    agg = BarAggregator("NQ", ["1m", "5m"])
    for second in range(0, 300, 30):
        agg.add_tick(tick(second, "20100"))

    bars = agg.add_tick(tick(300, "20200"))
    assert [b.timeframe for b in bars] == ["1m", "5m"]
    assert bars[0].close_time == bars[1].close_time


def test_five_minute_aggregates_the_whole_window() -> None:
    agg = BarAggregator("NQ", ["1m", "5m"])
    agg.add_tick(tick(10, "20100", 1))
    agg.add_tick(tick(130, "20150", 1))
    agg.add_tick(tick(290, "20080", 1))

    bars = agg.add_tick(tick(300, "20090"))
    five = next(b for b in bars if b.timeframe == "5m")
    assert five.open == Decimal("20100")
    assert five.high == Decimal("20150")
    assert five.low == Decimal("20080")
    assert five.close == Decimal("20080")
    assert five.volume == 3


def test_quiet_minutes_produce_no_synthetic_bars() -> None:
    agg = BarAggregator("NQ", ["1m"])
    agg.add_tick(tick(0, "20100"))
    bars = agg.add_tick(tick(400, "20200"))
    assert len(bars) == 1
    assert bars[0].open_time == START


def test_flush_closes_the_open_buckets() -> None:
    agg = BarAggregator("NQ", ["1m", "5m"])
    agg.add_tick(tick(10, "20100"))
    bars = agg.flush()
    assert [b.timeframe for b in bars] == ["1m", "5m"]
    assert agg.flush() == []


def test_out_of_order_ticks_are_rejected() -> None:
    agg = BarAggregator("NQ", ["1m"])
    agg.add_tick(tick(60, "20100"))
    with pytest.raises(ValueError, match="out of order"):
        agg.add_tick(tick(30, "20100"))


def test_unknown_timeframe_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unsupported timeframe"):
        BarAggregator("NQ", ["3m"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_aggregator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.feed.aggregator'`

- [ ] **Step 3: Write `aggregator.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from nq_agent.models import TIMEFRAME_SECONDS, Bar, Tick


@dataclass
class _Builder:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def update(self, tick: Tick) -> None:
        self.high = max(self.high, tick.price)
        self.low = min(self.low, tick.price)
        self.close = tick.price
        self.volume += tick.size


def _bucket_start(ts: datetime, seconds: int) -> datetime:
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


class BarAggregator:
    """Builds closed bars from ticks, one bucket per timeframe.

    Every timeframe is built directly from ticks rather than rolled up from
    the base timeframe. The result is identical when no bucket is empty, and
    strictly more correct when one is — a 5m bar still forms even if a
    constituent minute saw zero trades.
    """

    def __init__(self, symbol: str, timeframes: list[str]) -> None:
        for timeframe in timeframes:
            if timeframe not in TIMEFRAME_SECONDS:
                raise ValueError(f"unsupported timeframe: {timeframe}")
        self._symbol = symbol
        self._timeframes = sorted(timeframes, key=lambda tf: TIMEFRAME_SECONDS[tf])
        self._builders: dict[str, _Builder | None] = dict.fromkeys(self._timeframes)
        self._last_ts: datetime | None = None

    def _finish(self, timeframe: str, builder: _Builder) -> Bar:
        return Bar(
            symbol=self._symbol,
            timeframe=timeframe,
            open_time=builder.open_time,
            open=builder.open,
            high=builder.high,
            low=builder.low,
            close=builder.close,
            volume=builder.volume,
            closed=True,
        )

    def _ordered(self, bars: list[Bar]) -> list[Bar]:
        return sorted(bars, key=lambda b: (b.close_time, TIMEFRAME_SECONDS[b.timeframe]))

    def add_tick(self, tick: Tick) -> list[Bar]:
        if self._last_ts is not None and tick.ts < self._last_ts:
            raise ValueError(f"tick out of order: {tick.ts} < {self._last_ts}")
        self._last_ts = tick.ts

        closed: list[Bar] = []
        for timeframe in self._timeframes:
            seconds = TIMEFRAME_SECONDS[timeframe]
            start = _bucket_start(tick.ts, seconds)
            builder = self._builders[timeframe]

            if builder is not None and start > builder.open_time:
                closed.append(self._finish(timeframe, builder))
                builder = None

            if builder is None:
                self._builders[timeframe] = _Builder(
                    open_time=start,
                    open=tick.price,
                    high=tick.price,
                    low=tick.price,
                    close=tick.price,
                    volume=tick.size,
                )
            else:
                builder.update(tick)

        return self._ordered(closed)

    def flush(self) -> list[Bar]:
        closed: list[Bar] = []
        for timeframe in self._timeframes:
            builder = self._builders[timeframe]
            if builder is not None:
                closed.append(self._finish(timeframe, builder))
                self._builders[timeframe] = None
        return self._ordered(closed)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_aggregator.py -v`
Expected: 8 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/feed/aggregator.py tests/test_aggregator.py
git commit -m "feat: tick aggregator with deterministic multi-timeframe ordering"
```

---

### Task 6: DataFeed interface and replay feed

**Files:**
- Create: `src/nq_agent/feed/base.py`
- Create: `src/nq_agent/feed/replay.py`
- Create: `tests/fixtures/make_fixture.py`
- Create: `tests/fixtures/2026-07-15.jsonl` (generated)
- Test: `tests/test_replay_feed.py`

**Interfaces:**
- Consumes: `Bar`, `Tick` from `nq_agent.models`; `BarAggregator`; `SimClock`
- Produces: `DataFeed` ABC with `async get_bars(symbol, timeframe, start, end) -> list[Bar]`, `stream(symbol, timeframes, resume_from: datetime | None = None) -> AsyncIterator[Bar]`, `async close() -> None`. `ReplayFeed(fixture_path: Path, symbol: str, clock: SimClock | None = None)` also exposes `first_tick_time() -> datetime`.

`resume_from` is how crash recovery stays on one code path. A feed that receives it must emit history from that point before live data. `ReplayFeed` always replays the whole fixture, so it accepts the argument and ignores it; `DatabentoFeed` will use it to backfill. The engine decides which emitted bars count as warmup by comparing each bar's `close_time` against the same value.

Fixture line format, one JSON object per line:

```json
{"ts": "2026-07-15T13:30:00.250000+00:00", "price": "20100.25", "size": 2}
```

The real fixtures get recorded from Databento in a later plan. This task generates a deterministic synthetic day so every downstream test has something to run against today.

- [ ] **Step 1: Write the fixture generator**

`tests/fixtures/make_fixture.py`:

```python
"""Generate a deterministic synthetic trading day of NQ ticks.

Run: uv run python tests/fixtures/make_fixture.py
Deterministic by seed, so the committed fixture is reproducible.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SESSION_OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 America/New_York
SESSION_MINUTES = 420  # 09:30 to 16:30
TICKS_PER_MINUTE = 12
TICK_SIZE = Decimal("0.25")


def main() -> None:
    rng = random.Random(20260715)
    price = Decimal("20100.00")
    out = Path(__file__).parent / "2026-07-15.jsonl"

    with out.open("w", encoding="utf-8") as handle:
        for minute in range(SESSION_MINUTES):
            for slot in range(TICKS_PER_MINUTE):
                ts = SESSION_OPEN + timedelta(
                    minutes=minute, seconds=slot * (60 // TICKS_PER_MINUTE)
                )
                price += TICK_SIZE * rng.randint(-4, 4)
                handle.write(
                    json.dumps(
                        {
                            "ts": ts.isoformat(),
                            "price": str(price),
                            "size": rng.randint(1, 5),
                        }
                    )
                    + "\n"
                )

    print(f"wrote {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate and inspect the fixture**

```bash
uv run python tests/fixtures/make_fixture.py
wc -l tests/fixtures/2026-07-15.jsonl
head -1 tests/fixtures/2026-07-15.jsonl
```

Expected: `5040 tests/fixtures/2026-07-15.jsonl`, and a first line whose `ts` is `2026-07-15T13:30:00+00:00`.

- [ ] **Step 3: Write the failing test**

`tests/test_replay_feed.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

from nq_agent.clock import SimClock
from nq_agent.feed.replay import ReplayFeed

FIXTURE = Path("tests/fixtures/2026-07-15.jsonl")
OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)


async def test_stream_yields_only_closed_bars_in_close_time_order() -> None:
    feed = ReplayFeed(FIXTURE, "NQ")
    bars = [bar async for bar in feed.stream("NQ", ["1m", "5m"])]
    await feed.close()

    assert bars, "fixture produced no bars"
    assert all(bar.closed for bar in bars)
    close_times = [bar.close_time for bar in bars]
    assert close_times == sorted(close_times)


async def test_stream_emits_one_minute_before_five_minute_on_shared_boundary() -> None:
    feed = ReplayFeed(FIXTURE, "NQ")
    bars = [bar async for bar in feed.stream("NQ", ["1m", "5m"])]
    await feed.close()

    first_five = next(i for i, bar in enumerate(bars) if bar.timeframe == "5m")
    assert bars[first_five - 1].timeframe == "1m"
    assert bars[first_five - 1].close_time == bars[first_five].close_time


async def test_stream_advances_the_sim_clock_to_each_bar_close() -> None:
    clock = SimClock(OPEN)
    feed = ReplayFeed(FIXTURE, "NQ", clock=clock)

    seen: list[datetime] = []
    async for bar in feed.stream("NQ", ["1m"]):
        assert clock.now() == bar.close_time
        seen.append(clock.now())
    await feed.close()

    assert seen == sorted(seen)
    assert clock.now() == CLOSE


async def test_get_bars_returns_the_requested_window_only() -> None:
    feed = ReplayFeed(FIXTURE, "NQ")
    start = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 15, 14, 10, tzinfo=timezone.utc)

    bars = await feed.get_bars("NQ", "1m", start, end)
    await feed.close()

    assert len(bars) == 10
    assert bars[0].open_time == start
    assert bars[-1].open_time == datetime(2026, 7, 15, 14, 9, tzinfo=timezone.utc)
    assert all(bar.timeframe == "1m" for bar in bars)


async def test_final_partial_bucket_is_flushed() -> None:
    feed = ReplayFeed(FIXTURE, "NQ")
    bars = [bar async for bar in feed.stream("NQ", ["1m"])]
    await feed.close()
    assert bars[-1].close_time == CLOSE


async def test_resume_from_is_accepted_and_ignored_by_replay() -> None:
    feed = ReplayFeed(FIXTURE, "NQ")
    resumed = [
        bar
        async for bar in feed.stream("NQ", ["1m"], datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc))
    ]
    await feed.close()
    assert resumed[0].open_time == OPEN


def test_first_tick_time_reads_only_the_first_line() -> None:
    assert ReplayFeed(FIXTURE, "NQ").first_tick_time() == OPEN
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `uv run pytest tests/test_replay_feed.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.feed.replay'`

- [ ] **Step 5: Write `feed/base.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from nq_agent.models import Bar


class DataFeed(ABC):
    """Source of closed bars.

    Implementations never emit a partial bar. When the provider streams ticks,
    the aggregator holds each bucket until it closes. Every lookahead bug
    starts with a partial bar reaching the strategy.
    """

    @abstractmethod
    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        """Historical closed bars with open_time in [start, end)."""

    @abstractmethod
    def stream(
        self, symbol: str, timeframes: list[str], resume_from: datetime | None = None
    ) -> AsyncIterator[Bar]:
        """Closed bars across all requested timeframes, ordered by close time.

        When two timeframes close on the same boundary the shorter one is
        yielded first. A 5m arriving before its own final 1m is a lookahead bug.

        When resume_from is set the feed must emit history from that point
        before live data, so crash recovery and a cold start share one path.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any provider resources."""
```

- [ ] **Step 6: Write `feed/replay.py`**

```python
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SimClock
from nq_agent.feed.aggregator import BarAggregator
from nq_agent.feed.base import DataFeed
from nq_agent.models import Bar, Tick


class ReplayFeed(DataFeed):
    """Deterministic playback of a JSONL tick fixture.

    No network, no sleeping. When a SimClock is supplied it is advanced to each
    bar's close time immediately before that bar is yielded, so downstream
    components observe exactly the time the bar represents.
    """

    def __init__(self, fixture_path: Path, symbol: str, clock: SimClock | None = None) -> None:
        self._path = fixture_path
        self._symbol = symbol
        self._clock = clock

    def _ticks(self) -> Iterator[Tick]:
        with self._path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                record = json.loads(line)
                yield Tick(
                    symbol=self._symbol,
                    ts=datetime.fromisoformat(record["ts"]),
                    price=Decimal(record["price"]),
                    size=int(record["size"]),
                )

    def first_tick_time(self) -> datetime:
        """First timestamp in the fixture, read without parsing the whole file."""
        for tick in self._ticks():
            return tick.ts
        raise ValueError(f"replay fixture is empty: {self._path}")

    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        aggregator = BarAggregator(symbol, [timeframe])
        collected: list[Bar] = []
        for tick in self._ticks():
            collected.extend(aggregator.add_tick(tick))
        collected.extend(aggregator.flush())
        return [bar for bar in collected if start <= bar.open_time < end]

    async def stream(
        self, symbol: str, timeframes: list[str], resume_from: datetime | None = None
    ) -> AsyncIterator[Bar]:
        # resume_from is accepted for interface parity. A fixture always replays
        # in full, so the history a live feed would have to backfill is already here.
        aggregator = BarAggregator(symbol, timeframes)
        for tick in self._ticks():
            for bar in aggregator.add_tick(tick):
                if self._clock is not None:
                    self._clock.advance_to(bar.close_time)
                yield bar
        for bar in aggregator.flush():
            if self._clock is not None:
                self._clock.advance_to(bar.close_time)
            yield bar

    async def close(self) -> None:
        return None
```

`stream` is declared `def` on the ABC and `async def` on the implementation. An `async def` function containing `yield` is an async generator function — calling it returns an `AsyncIterator` without awaiting, which is exactly what the ABC signature promises.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_replay_feed.py -v`
Expected: 7 passed

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/feed/ tests/fixtures/ tests/test_replay_feed.py
git commit -m "feat: DataFeed interface and deterministic JSONL replay feed"
```

---

### Task 7: Strategy interface, Context, and the two reference strategies

**Files:**
- Create: `src/nq_agent/context.py`
- Create: `src/nq_agent/strategy/base.py`
- Create: `src/nq_agent/strategy/stub.py`
- Create: `src/nq_agent/strategy/always.py`
- Test: `tests/test_strategy.py`

**Interfaces:**
- Consumes: `Bar`, `Signal`, `Position`, `Direction`, `SignalIntent` from `nq_agent.models`; `Clock`, `SessionCalendar` from `nq_agent.clock`
- Produces: `Context(clock, calendar, history_bars)` with read properties `position`, `trades_taken`, `now`, `session_date`, `is_warmup`, method `bars(timeframe, count)`, and engine-only mutators `record_bar`, `set_position`, `set_trades_taken`, `set_warmup`. `Strategy` ABC with `name`, `required_timeframes`, `on_bar`, `on_session_start`, `on_session_end`, `get_state`, `restore_state`. `StubStrategy()`, `AlwaysStrategy(stop_offset=Decimal("10"), target_offset=Decimal("20"))`.

- [ ] **Step 1: Write the failing test**

`tests/test_strategy.py`:

```python
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Position, SignalIntent
from nq_agent.strategy.always import AlwaysStrategy
from nq_agent.strategy.stub import StubStrategy

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)


def calendar() -> SessionCalendar:
    return SessionCalendar("America/New_York", time(9, 30), time(16, 30))


def context(now: datetime = OPEN, history: int = 500) -> Context:
    return Context(SimClock(now), calendar(), history)


def bar(minute: int, close: str = "20100", timeframe: str = "1m") -> Bar:
    return Bar(
        symbol="NQ",
        timeframe=timeframe,
        open_time=OPEN + timedelta(minutes=minute),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
    )


def test_context_returns_most_recent_bars_oldest_first() -> None:
    ctx = context()
    for minute in range(5):
        ctx.record_bar(bar(minute, close=str(20100 + minute)))

    recent = ctx.bars("1m", 3)
    assert [b.close for b in recent] == [Decimal("20102"), Decimal("20103"), Decimal("20104")]


def test_context_separates_timeframes() -> None:
    ctx = context()
    ctx.record_bar(bar(0, timeframe="1m"))
    ctx.record_bar(bar(0, timeframe="5m"))
    assert len(ctx.bars("1m", 10)) == 1
    assert len(ctx.bars("5m", 10)) == 1


def test_context_history_is_bounded() -> None:
    ctx = context(history=3)
    for minute in range(10):
        ctx.record_bar(bar(minute))
    assert len(ctx.bars("1m", 100)) == 3


def test_context_exposes_session_date_from_the_clock() -> None:
    ctx = context(now=datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc))
    assert ctx.session_date == date(2026, 7, 15)


def test_context_position_and_counters_round_trip() -> None:
    ctx = context()
    assert ctx.position is None
    assert ctx.trades_taken == 0
    assert ctx.is_warmup is False

    position = Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        entry_price=Decimal("20100"),
        entry_time=OPEN,
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
    )
    ctx.set_position(position)
    ctx.set_trades_taken(2)
    ctx.set_warmup(True)

    assert ctx.position == position
    assert ctx.trades_taken == 2
    assert ctx.is_warmup is True


def test_stub_strategy_never_fires() -> None:
    strategy = StubStrategy()
    ctx = context()
    strategy.on_session_start(date(2026, 7, 15))
    for minute in range(20):
        assert strategy.on_bar(bar(minute), ctx) is None


def test_always_strategy_fires_once_per_session() -> None:
    strategy = AlwaysStrategy()
    ctx = context()
    strategy.on_session_start(date(2026, 7, 15))

    first = strategy.on_bar(bar(0, close="20100"), ctx)
    assert first is not None
    assert first.intent is SignalIntent.ENTRY
    assert first.direction is Direction.LONG
    assert first.entry_price == Decimal("20100")
    assert first.stop_price == Decimal("20090")
    assert first.target_price == Decimal("20120")
    assert first.quantity == 1

    assert strategy.on_bar(bar(1, close="20105"), ctx) is None
    assert strategy.on_bar(bar(2, close="20110"), ctx) is None


def test_always_strategy_ignores_non_base_timeframes() -> None:
    strategy = AlwaysStrategy()
    ctx = context()
    strategy.on_session_start(date(2026, 7, 15))
    assert strategy.on_bar(bar(0, timeframe="5m"), ctx) is None


def test_always_strategy_rearms_on_the_next_session() -> None:
    strategy = AlwaysStrategy()
    ctx = context()

    strategy.on_session_start(date(2026, 7, 15))
    assert strategy.on_bar(bar(0), ctx) is not None
    strategy.on_session_end(date(2026, 7, 15))

    strategy.on_session_start(date(2026, 7, 16))
    assert strategy.on_bar(bar(0), ctx) is not None


def test_always_strategy_state_round_trips() -> None:
    strategy = AlwaysStrategy()
    ctx = context()
    strategy.on_session_start(date(2026, 7, 15))
    assert strategy.on_bar(bar(0), ctx) is not None

    saved = strategy.get_state()

    restored = AlwaysStrategy()
    restored.on_session_start(date(2026, 7, 15))
    restored.restore_state(saved)
    assert restored.on_bar(bar(1), ctx) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_strategy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.context'`

- [ ] **Step 3: Write `context.py`**

```python
from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import date, datetime

from nq_agent.clock import Clock, SessionCalendar
from nq_agent.models import Bar, Position


class Context:
    """Read-only view handed to the strategy on every bar.

    The strategy sees only the properties. The mutators are the engine's, and
    the engine calls record_bar before on_bar, so the bar being processed is
    already the newest entry in its timeframe's history.
    """

    def __init__(self, clock: Clock, calendar: SessionCalendar, history_bars: int) -> None:
        self._clock = clock
        self._calendar = calendar
        self._history_bars = history_bars
        self._bars: dict[str, deque[Bar]] = {}
        self._position: Position | None = None
        self._trades_taken = 0
        self._warmup = False

    def bars(self, timeframe: str, count: int) -> Sequence[Bar]:
        history = self._bars.get(timeframe)
        if history is None:
            return []
        if count >= len(history):
            return list(history)
        return list(history)[-count:]

    @property
    def position(self) -> Position | None:
        return self._position

    @property
    def trades_taken(self) -> int:
        return self._trades_taken

    @property
    def now(self) -> datetime:
        return self._clock.now()

    @property
    def session_date(self) -> date:
        return self._calendar.session_date_for(self._clock.now())

    @property
    def is_warmup(self) -> bool:
        return self._warmup

    def record_bar(self, bar: Bar) -> None:
        history = self._bars.get(bar.timeframe)
        if history is None:
            history = deque(maxlen=self._history_bars)
            self._bars[bar.timeframe] = history
        history.append(bar)

    def set_position(self, position: Position | None) -> None:
        self._position = position

    def set_trades_taken(self, count: int) -> None:
        self._trades_taken = count

    def set_warmup(self, warmup: bool) -> None:
        self._warmup = warmup
```

- [ ] **Step 4: Write `strategy/base.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from nq_agent.context import Context
from nq_agent.models import Bar, Signal


class Strategy(ABC):
    """The one interface a real strategy implements.

    Every method is sync on purpose. The strategy does no network, no file
    access and no wall-clock reads — it takes time from the context. Sync
    signatures mean a strategy structurally cannot await a network call, so
    purity is enforced by the type system rather than by discipline.
    """

    name: str
    required_timeframes: list[str]

    @abstractmethod
    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        """Called once per closed bar. Return a Signal to trade, or None."""

    @abstractmethod
    def on_session_start(self, session_date: date) -> None:
        """Reset per-session state."""

    @abstractmethod
    def on_session_end(self, session_date: date) -> None:
        """Tear down per-session state."""

    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        """Serialisable internal state, for crash recovery."""

    @abstractmethod
    def restore_state(self, state: dict[str, Any]) -> None:
        """Rebuild internal state from get_state output."""
```

- [ ] **Step 5: Write `strategy/stub.py`**

```python
from __future__ import annotations

from datetime import date
from typing import Any

from nq_agent.context import Context
from nq_agent.models import Bar, Signal
from nq_agent.strategy.base import Strategy


class StubStrategy(Strategy):
    """Never fires. Proves the pipeline runs a full session without trading."""

    name = "stub"
    required_timeframes = ["1m", "5m"]

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        return None

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {}

    def restore_state(self, state: dict[str, Any]) -> None:
        return None
```

- [ ] **Step 6: Write `strategy/always.py`**

```python
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from nq_agent.context import Context
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.strategy.base import Strategy


class AlwaysStrategy(Strategy):
    """Fires one long entry on the first 1m bar of each session.

    Exists to prove the pipeline end to end. It is not a trading idea.
    """

    name = "always"
    required_timeframes = ["1m"]
    base_timeframe = "1m"

    def __init__(
        self,
        stop_offset: Decimal = Decimal("10"),
        target_offset: Decimal = Decimal("20"),
    ) -> None:
        self._stop_offset = stop_offset
        self._target_offset = target_offset
        self._fired = False

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe != self.base_timeframe or self._fired:
            return None

        self._fired = True
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            intent=SignalIntent.ENTRY,
            direction=Direction.LONG,
            entry_price=bar.close,
            stop_price=bar.close - self._stop_offset,
            target_price=bar.close + self._target_offset,
            quantity=1,
            reason="always strategy: first bar of session",
            metadata={"bar_open_time": bar.open_time.isoformat()},
        )

    def on_session_start(self, session_date: date) -> None:
        self._fired = False

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {"fired": self._fired}

    def restore_state(self, state: dict[str, Any]) -> None:
        self._fired = bool(state.get("fired", False))
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_strategy.py -v`
Expected: 10 passed

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/context.py src/nq_agent/strategy/ tests/test_strategy.py
git commit -m "feat: Strategy interface, read-only Context, stub and always strategies"
```

---

### Task 8: Executor interface and dry-run implementations

**Files:**
- Create: `src/nq_agent/execution/base.py`
- Create: `src/nq_agent/execution/dryrun.py`
- Test: `tests/test_executors.py`

**Interfaces:**
- Consumes: `Signal`, `OrderResult`, `SignalIntent`, `Direction` from `nq_agent.models`
- Produces: `Executor` ABC with attributes `name: str`, `account_id: str | None`, `enabled: bool` and methods `async execute(signal) -> OrderResult`, `async health_check() -> bool`. `NotifyExecutor(Executor)` adds `async alert(message: str) -> None`. `DryRunExecutor(name, account_id, enabled=True)`, `DryRunNotifier(name, enabled=True)` with inspectable `.sent: list[Signal]` and `.alerts: list[str]`.

Executor instances are per-account. A config entry listing three accounts becomes three instances at wiring time.

- [ ] **Step 1: Write the failing test**

`tests/test_executors.py`:

```python
from datetime import datetime, timezone
from decimal import Decimal

from nq_agent.execution.dryrun import DryRunExecutor, DryRunNotifier
from nq_agent.models import Direction, Signal, SignalIntent


def entry_signal() -> Signal:
    return Signal(
        timestamp=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.LONG,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
        quantity=1,
        reason="test",
    )


async def test_dry_run_executor_reports_success_with_its_account() -> None:
    executor = DryRunExecutor("dryrun", account_id="tradeify")
    result = await executor.execute(entry_signal())

    assert result.success is True
    assert result.executor_name == "dryrun:tradeify"
    assert result.account_id == "tradeify"
    assert result.error is None


async def test_dry_run_executor_records_what_it_was_asked_to_send() -> None:
    executor = DryRunExecutor("dryrun", account_id="mff")
    signal = entry_signal()
    await executor.execute(signal)
    assert executor.sent == [signal]


async def test_dry_run_executor_echoes_the_signal_in_raw_response() -> None:
    executor = DryRunExecutor("dryrun", account_id="mff")
    signal = entry_signal()
    result = await executor.execute(signal)
    assert result.raw_response["intent"] == "ENTRY"
    assert result.raw_response["direction"] == "LONG"
    assert result.raw_response["entry_price"] == "20100"


async def test_dry_run_notifier_has_no_account_and_records_alerts() -> None:
    notifier = DryRunNotifier("notify")
    result = await notifier.execute(entry_signal())
    assert result.executor_name == "notify"
    assert result.account_id is None

    await notifier.alert("executor signaltradeapp:mff failed")
    assert notifier.alerts == ["executor signaltradeapp:mff failed"]


async def test_health_check_passes_for_dry_run_components() -> None:
    assert await DryRunExecutor("dryrun", account_id="tradeify").health_check() is True
    assert await DryRunNotifier("notify").health_check() is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_executors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.execution.dryrun'`

- [ ] **Step 3: Write `execution/base.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod

from nq_agent.models import OrderResult, Signal


class Executor(ABC):
    """One destination for one account.

    A config entry listing several accounts expands into several instances at
    wiring time, so per-account enable/disable is just `enabled` on an instance
    and the router's fan-out stays uniform.

    Signals carry absolute prices. Converting them to points, ticks or dollars
    is this layer's job, which is what keeps strategy logic broker-agnostic.
    """

    name: str
    account_id: str | None
    enabled: bool

    @abstractmethod
    async def execute(self, signal: Signal) -> OrderResult:
        """Send the signal. Must not raise — return a failed OrderResult instead."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Cheap liveness probe, called once at startup."""


class NotifyExecutor(Executor):
    """An executor that also carries out-of-band alerts.

    The router runs notify executors first, because the human reading them is
    the slow leg, and uses `alert` to report partial fan-out failures.
    """

    @abstractmethod
    async def alert(self, message: str) -> None:
        """Push an operational message that is not itself a trade."""
```

- [ ] **Step 4: Write `execution/dryrun.py`**

```python
from __future__ import annotations

import logging

from nq_agent.execution.base import Executor, NotifyExecutor
from nq_agent.models import OrderResult, Signal

logger = logging.getLogger(__name__)


def _describe(signal: Signal) -> dict[str, str]:
    described = {
        "signal_id": signal.id,
        "intent": signal.intent.value,
        "direction": signal.direction.value,
        "symbol": signal.symbol,
        "quantity": str(signal.quantity),
    }
    for field in ("entry_price", "stop_price", "target_price"):
        value = getattr(signal, field)
        if value is not None:
            described[field] = str(value)
    return described


class DryRunExecutor(Executor):
    """Logs instead of trading. Produces the same OrderResult shape as the real thing."""

    def __init__(self, name: str, account_id: str | None = None, enabled: bool = True) -> None:
        self.name = f"{name}:{account_id}" if account_id else name
        self.account_id = account_id
        self.enabled = enabled
        self.sent: list[Signal] = []

    async def execute(self, signal: Signal) -> OrderResult:
        self.sent.append(signal)
        payload = _describe(signal)
        logger.info("dry run execute %s %s", self.name, payload)
        return OrderResult(
            signal_id=signal.id,
            executor_name=self.name,
            success=True,
            account_id=self.account_id,
            latency_ms=0,
            raw_response=payload,
        )

    async def health_check(self) -> bool:
        return True


class DryRunNotifier(NotifyExecutor):
    """Stands in for ntfy or Pushover until one is chosen."""

    def __init__(self, name: str, enabled: bool = True) -> None:
        self.name = name
        self.account_id = None
        self.enabled = enabled
        self.sent: list[Signal] = []
        self.alerts: list[str] = []

    async def execute(self, signal: Signal) -> OrderResult:
        self.sent.append(signal)
        payload = _describe(signal)
        logger.info("dry run notify %s %s", self.name, payload)
        return OrderResult(
            signal_id=signal.id,
            executor_name=self.name,
            success=True,
            account_id=None,
            latency_ms=0,
            raw_response=payload,
        )

    async def alert(self, message: str) -> None:
        self.alerts.append(message)
        logger.warning("dry run alert %s: %s", self.name, message)

    async def health_check(self) -> bool:
        return True
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_executors.py -v`
Expected: 5 passed

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/execution/ tests/test_executors.py
git commit -m "feat: per-account Executor interface with dry-run implementations"
```

---

### Task 9: Router

**Files:**
- Create: `src/nq_agent/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `Executor`, `NotifyExecutor`; `Signal`, `OrderResult`; `Journal`
- Produces: `Router(executors, journal, executor_timeout, notify_timeout, partial_fan, enabled_accounts: Callable[[], set[str] | None] | None = None)` with `async dispatch(signal: Signal, session_date: date) -> list[OrderResult]` and a public `enabled: list[Executor]` property.

`enabled` is a property, not a cached list, and it calls `enabled_accounts()` fresh every time. That callable is the account registry from Task 10, so an account switched off mid-session takes effect on the next signal without a restart. The engine reads `len(router.enabled)` when asking the risk layer whether any destination is left.

- [ ] **Step 1: Write the failing test**

`tests/test_router.py`:

```python
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SimClock
from nq_agent.execution.base import Executor, NotifyExecutor
from nq_agent.execution.dryrun import DryRunExecutor, DryRunNotifier
from nq_agent.journal import Journal
from nq_agent.models import Direction, OrderResult, Signal, SignalIntent
from nq_agent.router import Router

SESSION = date(2026, 7, 15)


def signal() -> Signal:
    return Signal(
        timestamp=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.LONG,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
        quantity=1,
        reason="test",
    )


def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path, SimClock(datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc)))


class SlowExecutor(Executor):
    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.account_id = name
        self.enabled = True
        self._delay = delay

    async def execute(self, sig: Signal) -> OrderResult:
        await asyncio.sleep(self._delay)
        return OrderResult(signal_id=sig.id, executor_name=self.name, success=True)

    async def health_check(self) -> bool:
        return True


class ExplodingExecutor(Executor):
    def __init__(self, name: str) -> None:
        self.name = name
        self.account_id = name
        self.enabled = True

    async def execute(self, sig: Signal) -> OrderResult:
        raise RuntimeError("broker exploded")

    async def health_check(self) -> bool:
        return False


class OrderRecordingNotifier(DryRunNotifier):
    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__(name)
        self._log = log

    async def execute(self, sig: Signal) -> OrderResult:
        await asyncio.sleep(0.02)
        self._log.append("notify")
        return await super().execute(sig)


class OrderRecordingExecutor(DryRunExecutor):
    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__(name, account_id="acct")
        self._log = log

    async def execute(self, sig: Signal) -> OrderResult:
        self._log.append("broker")
        return await super().execute(sig)


async def test_every_enabled_executor_receives_the_signal(tmp_path: Path) -> None:
    a = DryRunExecutor("broker", account_id="tradeify")
    b = DryRunExecutor("broker", account_id="mff")
    router = Router([a, b], journal(tmp_path), 1.0, 1.0, "continue")

    results = await router.dispatch(signal(), SESSION)

    assert {r.executor_name for r in results} == {"broker:tradeify", "broker:mff"}
    assert all(r.success for r in results)


async def test_disabled_executors_are_skipped(tmp_path: Path) -> None:
    live = DryRunExecutor("broker", account_id="tradeify")
    dark = DryRunExecutor("broker", account_id="mff", enabled=False)
    router = Router([live, dark], journal(tmp_path), 1.0, 1.0, "continue")

    results = await router.dispatch(signal(), SESSION)

    assert [r.executor_name for r in results] == ["broker:tradeify"]
    assert dark.sent == []


async def test_notify_runs_before_the_rest(tmp_path: Path) -> None:
    order: list[str] = []
    notifier = OrderRecordingNotifier("notify", order)
    broker = OrderRecordingExecutor("broker", order)
    router = Router([broker, notifier], journal(tmp_path), 1.0, 1.0, "continue")

    await router.dispatch(signal(), SESSION)

    assert order == ["notify", "broker"]


async def test_a_timeout_becomes_a_failed_result_without_blocking_others(
    tmp_path: Path,
) -> None:
    slow = SlowExecutor("slow", delay=5.0)
    fast = DryRunExecutor("broker", account_id="tradeify")
    router = Router([slow, fast], journal(tmp_path), executor_timeout=0.05,
                    notify_timeout=1.0, partial_fan="continue")

    results = await router.dispatch(signal(), SESSION)

    by_name = {r.executor_name: r for r in results}
    assert by_name["slow"].success is False
    assert by_name["slow"].error == "timeout"
    assert by_name["broker:tradeify"].success is True


async def test_an_exception_becomes_a_failed_result(tmp_path: Path) -> None:
    router = Router(
        [ExplodingExecutor("boom"), DryRunExecutor("broker", account_id="mff")],
        journal(tmp_path),
        1.0,
        1.0,
        "continue",
    )

    results = await router.dispatch(signal(), SESSION)

    by_name = {r.executor_name: r for r in results}
    assert by_name["boom"].success is False
    assert "broker exploded" in (by_name["boom"].error or "")
    assert by_name["broker:mff"].success is True


async def test_continue_mode_does_not_alert(tmp_path: Path) -> None:
    notifier = DryRunNotifier("notify")
    router = Router(
        [ExplodingExecutor("boom"), notifier], journal(tmp_path), 1.0, 1.0, "continue"
    )

    await router.dispatch(signal(), SESSION)

    assert notifier.alerts == []


async def test_alert_only_mode_alerts_on_partial_failure(tmp_path: Path) -> None:
    notifier = DryRunNotifier("notify")
    router = Router(
        [ExplodingExecutor("boom"), notifier], journal(tmp_path), 1.0, 1.0, "alert_only"
    )

    await router.dispatch(signal(), SESSION)

    assert len(notifier.alerts) == 1
    assert "boom" in notifier.alerts[0]


async def test_alert_only_mode_stays_quiet_when_everything_succeeds(tmp_path: Path) -> None:
    notifier = DryRunNotifier("notify")
    router = Router(
        [DryRunExecutor("broker", account_id="mff"), notifier],
        journal(tmp_path),
        1.0,
        1.0,
        "alert_only",
    )

    await router.dispatch(signal(), SESSION)

    assert notifier.alerts == []


async def test_every_result_is_journaled_including_failures(tmp_path: Path) -> None:
    router = Router(
        [ExplodingExecutor("boom"), DryRunExecutor("broker", account_id="mff")],
        journal(tmp_path),
        1.0,
        1.0,
        "continue",
    )

    await router.dispatch(signal(), SESSION)

    lines = (tmp_path / "2026-07-15.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert all('"event": "order_result"' in line for line in lines)


async def test_latency_is_recorded(tmp_path: Path) -> None:
    router = Router([SlowExecutor("slow", delay=0.05)], journal(tmp_path), 1.0, 1.0, "continue")
    results = await router.dispatch(signal(), SESSION)
    assert results[0].latency_ms >= 40


async def test_account_registry_filters_enabled_executors(tmp_path: Path) -> None:
    allowed: set[str] | None = {"tradeify"}
    router = Router(
        [
            DryRunExecutor("broker", account_id="tradeify"),
            DryRunExecutor("broker", account_id="mff"),
            DryRunNotifier("notify"),
        ],
        journal(tmp_path),
        1.0,
        1.0,
        "continue",
        enabled_accounts=lambda: allowed,
    )

    results = await router.dispatch(signal(), SESSION)
    assert {r.executor_name for r in results} == {"broker:tradeify", "notify"}

    allowed = {"tradeify", "mff"}
    results = await router.dispatch(signal(), SESSION)
    assert {r.executor_name for r in results} == {"broker:tradeify", "broker:mff", "notify"}


async def test_no_registry_means_every_account_is_enabled(tmp_path: Path) -> None:
    router = Router(
        [DryRunExecutor("broker", account_id="tradeify")],
        journal(tmp_path),
        1.0,
        1.0,
        "continue",
        enabled_accounts=lambda: None,
    )
    assert len(router.enabled) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.router'`

- [ ] **Step 3: Write `router.py`**

```python
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import date

from nq_agent.execution.base import Executor, NotifyExecutor
from nq_agent.journal import Journal
from nq_agent.models import OrderResult, Signal

logger = logging.getLogger(__name__)


class Router:
    """Fans one Signal out to N executors.

    Notify executors run first and are awaited to completion or timeout — the
    human on the manual account is the slow leg and gets the head start. The
    remaining executors then run concurrently, each with an independent
    timeout, so one failing never blocks another.

    asyncio.TaskGroup is deliberately not used: it cancels siblings on failure,
    which is the exact behaviour this must avoid.
    """

    def __init__(
        self,
        executors: list[Executor],
        journal: Journal,
        executor_timeout: float,
        notify_timeout: float,
        partial_fan: str,
        enabled_accounts: Callable[[], set[str] | None] | None = None,
    ) -> None:
        self._executors = executors
        self._journal = journal
        self._executor_timeout = executor_timeout
        self._notify_timeout = notify_timeout
        self._partial_fan = partial_fan
        self._enabled_accounts = enabled_accounts

    @property
    def enabled(self) -> list[Executor]:
        """Live destinations, recomputed on every read.

        The account allow-list is queried fresh each time so an account can be
        disabled mid-session without restarting the process.
        """
        allowed = self._enabled_accounts() if self._enabled_accounts else None
        return [
            executor
            for executor in self._executors
            if executor.enabled
            and (allowed is None or executor.account_id is None or executor.account_id in allowed)
        ]

    def _notifiers(self) -> list[NotifyExecutor]:
        return [e for e in self.enabled if isinstance(e, NotifyExecutor)]

    def _brokers(self) -> list[Executor]:
        return [e for e in self.enabled if not isinstance(e, NotifyExecutor)]

    async def _run_one(
        self, executor: Executor, signal: Signal, timeout: float
    ) -> OrderResult:
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(executor.execute(signal), timeout)
        except asyncio.TimeoutError:
            elapsed = int((time.perf_counter() - started) * 1000)
            return OrderResult(
                signal_id=signal.id,
                executor_name=executor.name,
                success=False,
                account_id=executor.account_id,
                latency_ms=elapsed,
                error="timeout",
            )
        except Exception as exc:  # noqa: BLE001 - an executor must never break the fan-out
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.exception("executor %s raised", executor.name)
            return OrderResult(
                signal_id=signal.id,
                executor_name=executor.name,
                success=False,
                account_id=executor.account_id,
                latency_ms=elapsed,
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        if result.latency_ms:
            return result
        return result.model_copy(update={"latency_ms": elapsed})

    async def _run_group(
        self, executors: list[Executor], signal: Signal, timeout: float
    ) -> list[OrderResult]:
        if not executors:
            return []
        return list(
            await asyncio.gather(
                *(self._run_one(executor, signal, timeout) for executor in executors)
            )
        )

    async def dispatch(self, signal: Signal, session_date: date) -> list[OrderResult]:
        notifiers = self._notifiers()
        results = await self._run_group(list(notifiers), signal, self._notify_timeout)
        results.extend(await self._run_group(self._brokers(), signal, self._executor_timeout))

        for result in results:
            await self._journal.write(
                "order_result",
                session_date,
                signal_id=result.signal_id,
                executor_name=result.executor_name,
                account_id=result.account_id,
                success=result.success,
                latency_ms=result.latency_ms,
                error=result.error,
            )

        failed = [result for result in results if not result.success]
        if failed and self._partial_fan == "alert_only":
            names = ", ".join(result.executor_name for result in failed)
            message = f"partial fan-out on signal {signal.id}: {names} failed"
            for notifier in notifiers:
                await notifier.alert(message)
            await self._journal.write(
                "executor_alert", session_date, signal_id=signal.id, detail=message
            )

        return results
```

Latency uses `time.perf_counter`, which measures a duration rather than reading wall time. That does not violate the clock rule — nothing here needs to know what time it is.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_router.py -v`
Expected: 12 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/router.py tests/test_router.py
git commit -m "feat: router with notify-first ordering and isolated executor timeouts"
```

---

### Task 10: Risk layer

**Files:**
- Create: `src/nq_agent/risk/accounts.py`
- Create: `src/nq_agent/risk/limits.py`
- Test: `tests/test_risk.py`

**Interfaces:**
- Consumes: `Signal`, `SignalIntent`, `RiskVeto`, `VetoReason`; `SessionCalendar`
- Produces: `AccountRegistry(config_path: Path)` with `enabled_accounts() -> set[str]`, re-reading the file on every call. `RiskManager(calendar, max_trades_per_day, duplicate_window_seconds, kill_switch_path)` with `check(signal: Signal, trades_taken: int, enabled_executor_count: int) -> RiskVeto | None` and `record_accepted(signal: Signal) -> None`.

- [ ] **Step 1: Write the failing test**

`tests/test_risk.py`:

```python
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SessionCalendar
from nq_agent.models import Direction, Signal, SignalIntent, VetoReason
from nq_agent.risk.accounts import AccountRegistry
from nq_agent.risk.limits import RiskManager

IN_SESSION = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)  # 10:00 New York
AFTER_CUTOFF = datetime(2026, 7, 15, 20, 45, tzinfo=timezone.utc)  # 16:45 New York
BEFORE_OPEN = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)  # 08:00 New York


def calendar() -> SessionCalendar:
    return SessionCalendar("America/New_York", time(9, 30), time(16, 30))


def manager(kill_switch: Path, max_trades: int = 2, window: int = 60) -> RiskManager:
    return RiskManager(
        calendar=calendar(),
        max_trades_per_day=max_trades,
        duplicate_window_seconds=window,
        kill_switch_path=kill_switch,
    )


def entry(at: datetime = IN_SESSION, direction: Direction = Direction.LONG) -> Signal:
    long_side = direction is Direction.LONG
    return Signal(
        timestamp=at,
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=direction,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090") if long_side else Decimal("20110"),
        target_price=Decimal("20120") if long_side else Decimal("20080"),
        quantity=1,
        reason="test",
    )


def flatten(at: datetime = AFTER_CUTOFF) -> Signal:
    return Signal(
        timestamp=at,
        symbol="NQ",
        intent=SignalIntent.FLATTEN,
        direction=Direction.LONG,
        quantity=1,
        reason="session cutoff",
    )


def test_clean_entry_passes(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt")
    assert risk.check(entry(), trades_taken=0, enabled_executor_count=3) is None


def test_max_trades_vetoes(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt", max_trades=2)
    veto = risk.check(entry(), trades_taken=2, enabled_executor_count=3)
    assert veto is not None
    assert veto.reason is VetoReason.MAX_TRADES


def test_past_cutoff_vetoes(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt")
    veto = risk.check(entry(at=AFTER_CUTOFF), trades_taken=0, enabled_executor_count=3)
    assert veto is not None
    assert veto.reason is VetoReason.PAST_CUTOFF


def test_before_open_vetoes_as_session_closed(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt")
    veto = risk.check(entry(at=BEFORE_OPEN), trades_taken=0, enabled_executor_count=3)
    assert veto is not None
    assert veto.reason is VetoReason.SESSION_CLOSED


def test_kill_switch_file_vetoes_and_is_not_cached(tmp_path: Path) -> None:
    halt = tmp_path / "halt"
    risk = manager(halt)
    assert risk.check(entry(), trades_taken=0, enabled_executor_count=3) is None

    halt.write_text("halt")
    veto = risk.check(entry(), trades_taken=0, enabled_executor_count=3)
    assert veto is not None
    assert veto.reason is VetoReason.KILL_SWITCH

    halt.unlink()
    assert risk.check(entry(), trades_taken=0, enabled_executor_count=3) is None


def test_no_enabled_executors_vetoes(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt")
    veto = risk.check(entry(), trades_taken=0, enabled_executor_count=0)
    assert veto is not None
    assert veto.reason is VetoReason.ACCOUNT_DISABLED


def test_duplicate_within_window_is_rejected(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt", window=60)
    first = entry()
    assert risk.check(first, 0, 3) is None
    risk.record_accepted(first)

    second = entry(at=IN_SESSION + timedelta(seconds=30))
    veto = risk.check(second, 1, 3)
    assert veto is not None
    assert veto.reason is VetoReason.DUPLICATE_SIGNAL


def test_duplicate_outside_window_is_allowed(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt", window=60)
    first = entry()
    risk.record_accepted(first)
    later = entry(at=IN_SESSION + timedelta(seconds=61))
    assert risk.check(later, 1, 3) is None


def test_opposite_direction_within_window_is_allowed(tmp_path: Path) -> None:
    risk = manager(tmp_path / "halt", window=60)
    risk.record_accepted(entry())
    other = entry(at=IN_SESSION + timedelta(seconds=10), direction=Direction.SHORT)
    assert risk.check(other, 1, 3) is None


def test_flatten_bypasses_every_check(tmp_path: Path) -> None:
    halt = tmp_path / "halt"
    halt.write_text("halt")
    risk = manager(halt, max_trades=0)
    assert risk.check(flatten(), trades_taken=99, enabled_executor_count=0) is None


def test_account_registry_rereads_the_file(tmp_path: Path) -> None:
    config = tmp_path / "accounts.yaml"
    config.write_text("tradeify: true\nmff: true\nfundednext: false\n")
    registry = AccountRegistry(config)
    assert registry.enabled_accounts() == {"tradeify", "mff"}

    config.write_text("tradeify: false\nmff: true\nfundednext: true\n")
    assert registry.enabled_accounts() == {"mff", "fundednext"}


def test_missing_account_file_returns_none_meaning_no_overrides(tmp_path: Path) -> None:
    registry = AccountRegistry(tmp_path / "absent.yaml")
    assert registry.enabled_accounts() is None
```

`AccountRegistry.enabled_accounts` is what gets passed to `Router(enabled_accounts=...)` in Task 14. The router owns the filtering; the registry only answers the question.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_risk.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.risk.accounts'`

- [ ] **Step 3: Write `risk/accounts.py`**

```python
from __future__ import annotations

from pathlib import Path

import yaml


class AccountRegistry:
    """Per-account enable/disable, re-read from disk on every call.

    Reading at signal time rather than at startup is the whole point: an
    account can be switched off mid-session without restarting the process.

    A missing file means "no overrides" and returns None, which callers treat
    as every configured account being enabled.
    """

    def __init__(self, config_path: Path) -> None:
        self._path = config_path

    def enabled_accounts(self) -> set[str] | None:
        if not self._path.exists():
            return None
        loaded = yaml.safe_load(self._path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"account config must contain a mapping: {self._path}")
        return {name for name, enabled in loaded.items() if bool(enabled)}
```

- [ ] **Step 4: Write `risk/limits.py`**

```python
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from nq_agent.clock import SessionCalendar
from nq_agent.models import Direction, RiskVeto, Signal, SignalIntent, VetoReason


class RiskManager:
    """Sits between strategy and router. Vetoes only, never modifies a signal.

    FLATTEN bypasses every check. A kill switch that traps you in an open
    position is worse than no kill switch.
    """

    def __init__(
        self,
        calendar: SessionCalendar,
        max_trades_per_day: int,
        duplicate_window_seconds: int,
        kill_switch_path: Path,
    ) -> None:
        self._calendar = calendar
        self._max_trades = max_trades_per_day
        self._window = timedelta(seconds=duplicate_window_seconds)
        self._kill_switch_path = kill_switch_path
        self._recent: list[tuple[datetime, str, Direction]] = []

    def _is_duplicate(self, signal: Signal) -> bool:
        cutoff = signal.timestamp - self._window
        self._recent = [entry for entry in self._recent if entry[0] > cutoff]
        return any(
            symbol == signal.symbol and direction == signal.direction
            for _, symbol, direction in self._recent
        )

    def record_accepted(self, signal: Signal) -> None:
        if signal.intent is SignalIntent.ENTRY:
            self._recent.append((signal.timestamp, signal.symbol, signal.direction))

    def check(
        self, signal: Signal, trades_taken: int, enabled_executor_count: int
    ) -> RiskVeto | None:
        if signal.intent is SignalIntent.FLATTEN:
            return None

        def veto(reason: VetoReason, detail: str) -> RiskVeto:
            return RiskVeto(signal_id=signal.id, reason=reason, detail=detail)

        if self._kill_switch_path.exists():
            return veto(VetoReason.KILL_SWITCH, f"halt file present at {self._kill_switch_path}")

        if not self._calendar.is_session_open(signal.timestamp):
            if self._calendar.is_before_cutoff(signal.timestamp):
                return veto(VetoReason.SESSION_CLOSED, "signal arrived before session open")
            return veto(VetoReason.PAST_CUTOFF, "signal arrived at or after session cutoff")

        if trades_taken >= self._max_trades:
            return veto(
                VetoReason.MAX_TRADES,
                f"{trades_taken} trades already taken, limit is {self._max_trades}",
            )

        if enabled_executor_count == 0:
            return veto(VetoReason.ACCOUNT_DISABLED, "no enabled executor for any account")

        if self._is_duplicate(signal):
            return veto(
                VetoReason.DUPLICATE_SIGNAL,
                f"same symbol and direction within {self._window.total_seconds():.0f}s",
            )

        return None
```

The kill switch is a `Path.exists()` call on every check, deliberately uncached — a stale cache would let a trade through after you reached for the switch. That single `stat` is the one filesystem touch in the risk path.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_risk.py -v`
Expected: 12 passed

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/risk/ tests/test_risk.py
git commit -m "feat: veto-only risk layer with uncached kill switch"
```

---

### Task 11: Position tracker

**Files:**
- Create: `src/nq_agent/position.py`
- Test: `tests/test_position.py`

**Interfaces:**
- Consumes: `Bar`, `Signal`, `Position`, `PositionClose`, `Direction`, `SignalIntent`
- Produces: `PositionTracker()` with property `position: Position | None`, `on_signal(signal) -> None`, `on_bar(bar) -> PositionClose | None`, `flatten(price: Decimal, at: datetime) -> PositionClose | None`, `restore(position: Position | None) -> None`.

- [ ] **Step 1: Write the failing test**

`tests/test_position.py`:

```python
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from nq_agent.models import Bar, Direction, Position, Signal, SignalIntent
from nq_agent.position import PositionTracker

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)


def bar(minute: int, high: str, low: str, close: str) -> Bar:
    return Bar(
        symbol="NQ",
        timeframe="1m",
        open_time=OPEN + timedelta(minutes=minute),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10,
    )


def long_entry() -> Signal:
    return Signal(
        timestamp=OPEN + timedelta(minutes=1),
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.LONG,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
        quantity=1,
        reason="test",
    )


def short_entry() -> Signal:
    return Signal(
        timestamp=OPEN + timedelta(minutes=1),
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.SHORT,
        entry_price=Decimal("20100"),
        stop_price=Decimal("20110"),
        target_price=Decimal("20080"),
        quantity=1,
        reason="test",
    )


def test_starts_flat() -> None:
    assert PositionTracker().position is None


def test_entry_signal_opens_a_position_at_the_signal_price() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    position = tracker.position
    assert position is not None
    assert position.direction is Direction.LONG
    assert position.entry_price == Decimal("20100")
    assert position.entry_time == OPEN + timedelta(minutes=1)


def test_untouched_bar_leaves_the_position_open() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())
    assert tracker.on_bar(bar(2, high="20115", low="20095", close="20110")) is None
    assert tracker.position is not None


def test_long_target_touch_closes_at_the_target() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    closed = tracker.on_bar(bar(2, high="20125", low="20098", close="20122"))
    assert closed is not None
    assert closed.exit_reason == "TARGET"
    assert closed.exit_price == Decimal("20120")
    assert tracker.position is None


def test_long_stop_touch_closes_at_the_stop() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    closed = tracker.on_bar(bar(2, high="20105", low="20085", close="20088"))
    assert closed is not None
    assert closed.exit_reason == "STOP"
    assert closed.exit_price == Decimal("20090")


def test_stop_wins_when_one_bar_touches_both() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    closed = tracker.on_bar(bar(2, high="20130", low="20080", close="20125"))
    assert closed is not None
    assert closed.exit_reason == "STOP"


def test_short_stop_and_target_are_mirrored() -> None:
    tracker = PositionTracker()
    tracker.on_signal(short_entry())
    closed = tracker.on_bar(bar(2, high="20105", low="20075", close="20078"))
    assert closed is not None
    assert closed.exit_reason == "TARGET"
    assert closed.exit_price == Decimal("20080")

    tracker = PositionTracker()
    tracker.on_signal(short_entry())
    closed = tracker.on_bar(bar(2, high="20115", low="20095", close="20112"))
    assert closed is not None
    assert closed.exit_reason == "STOP"
    assert closed.exit_price == Decimal("20110")


def test_flatten_closes_at_the_supplied_price() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())

    closed = tracker.flatten(Decimal("20107"), OPEN + timedelta(minutes=400))
    assert closed is not None
    assert closed.exit_reason == "FLATTEN"
    assert closed.exit_price == Decimal("20107")
    assert tracker.position is None


def test_flatten_while_flat_is_a_no_op() -> None:
    assert PositionTracker().flatten(Decimal("20100"), OPEN) is None


def test_bars_are_ignored_while_flat() -> None:
    assert PositionTracker().on_bar(bar(2, high="99999", low="1", close="20100")) is None


def test_a_second_entry_while_open_is_ignored() -> None:
    tracker = PositionTracker()
    tracker.on_signal(long_entry())
    first = tracker.position
    tracker.on_signal(short_entry())
    assert tracker.position == first


def test_restore_reinstates_a_position() -> None:
    tracker = PositionTracker()
    position = Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        entry_price=Decimal("20100"),
        entry_time=OPEN,
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
    )
    tracker.restore(position)
    assert tracker.position == position

    closed = tracker.on_bar(bar(2, high="20105", low="20085", close="20088"))
    assert closed is not None
    assert closed.exit_reason == "STOP"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_position.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.position'`

- [ ] **Step 3: Write `position.py`**

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from nq_agent.models import Bar, Direction, Position, PositionClose, Signal, SignalIntent


class PositionTracker:
    """Simulated position state, driven by signals and bars.

    When a single bar's range touches both the stop and the target, the stop
    wins. Bar data cannot tell us which came first, so the tracker takes the
    pessimistic reading rather than guessing.
    """

    def __init__(self) -> None:
        self._position: Position | None = None

    @property
    def position(self) -> Position | None:
        return self._position

    def restore(self, position: Position | None) -> None:
        self._position = position

    def on_signal(self, signal: Signal) -> None:
        if signal.intent is not SignalIntent.ENTRY or self._position is not None:
            return
        assert signal.entry_price is not None
        assert signal.stop_price is not None
        assert signal.target_price is not None
        self._position = Position(
            symbol=signal.symbol,
            direction=signal.direction,
            quantity=signal.quantity,
            entry_price=signal.entry_price,
            entry_time=signal.timestamp,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
        )

    def on_bar(self, bar: Bar) -> PositionClose | None:
        position = self._position
        if position is None or bar.symbol != position.symbol:
            return None

        if position.direction is Direction.LONG:
            stop_hit = bar.low <= position.stop_price
            target_hit = bar.high >= position.target_price
        else:
            stop_hit = bar.high >= position.stop_price
            target_hit = bar.low <= position.target_price

        if stop_hit:
            return self._close(position, position.stop_price, bar.close_time, "STOP")
        if target_hit:
            return self._close(position, position.target_price, bar.close_time, "TARGET")
        return None

    def flatten(self, price: Decimal, at: datetime) -> PositionClose | None:
        position = self._position
        if position is None:
            return None
        return self._close(position, price, at, "FLATTEN")

    def _close(
        self, position: Position, price: Decimal, at: datetime, reason: str
    ) -> PositionClose:
        self._position = None
        return PositionClose(
            position=position, exit_price=price, exit_time=at, exit_reason=reason
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_position.py -v`
Expected: 12 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/position.py tests/test_position.py
git commit -m "feat: simulated position tracker with stop-wins exit resolution"
```

---

### Task 12: Session manager and cutoff flatten

**Files:**
- Create: `src/nq_agent/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `Strategy`; `SessionCalendar`; `Journal`; `Bar`, `Position`, `Signal`, `SignalIntent`
- Produces: `SessionManager(strategy, calendar, journal)` with `async on_bar(bar: Bar, position: Position | None) -> Signal | None`, property `current_session_date: date | None`, and `async end_session() -> None`.

- [ ] **Step 1: Write the failing test**

`tests/test_session.py`:

```python
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.journal import Journal
from nq_agent.models import Bar, Direction, Position, SignalIntent
from nq_agent.session import SessionManager
from nq_agent.strategy.always import AlwaysStrategy

OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 New York
CUTOFF = datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)  # 16:30 New York


def calendar() -> SessionCalendar:
    return SessionCalendar("America/New_York", time(9, 30), time(16, 30))


def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path, SimClock(OPEN))


def bar_closing_at(close_time: datetime, close: str = "20100") -> Bar:
    return Bar(
        symbol="NQ",
        timeframe="1m",
        open_time=close_time - timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
    )


def open_position() -> Position:
    return Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=2,
        entry_price=Decimal("20100"),
        entry_time=OPEN,
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
    )


async def test_first_bar_starts_the_session(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    manager = SessionManager(strategy, calendar(), journal(tmp_path))

    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)

    assert manager.current_session_date == date(2026, 7, 15)
    assert '"event": "session_start"' in (tmp_path / "2026-07-15.jsonl").read_text()


async def test_session_rollover_ends_the_old_and_starts_the_new(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    manager = SessionManager(strategy, calendar(), journal(tmp_path))

    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)
    await manager.on_bar(bar_closing_at(OPEN + timedelta(days=1, minutes=1)), None)

    assert manager.current_session_date == date(2026, 7, 16)
    assert '"event": "session_end"' in (tmp_path / "2026-07-15.jsonl").read_text()
    assert '"event": "session_start"' in (tmp_path / "2026-07-16.jsonl").read_text()


async def test_no_flatten_before_cutoff(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))
    result = await manager.on_bar(bar_closing_at(CUTOFF - timedelta(minutes=1)), open_position())
    assert result is None


async def test_no_flatten_when_flat(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))
    assert await manager.on_bar(bar_closing_at(CUTOFF), None) is None


async def test_flatten_emitted_at_cutoff_with_an_open_position(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))

    signal = await manager.on_bar(bar_closing_at(CUTOFF), open_position())

    assert signal is not None
    assert signal.intent is SignalIntent.FLATTEN
    assert signal.direction is Direction.LONG
    assert signal.quantity == 2
    assert signal.entry_price is None
    assert signal.timestamp == CUTOFF


async def test_flatten_is_emitted_only_once_per_session(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))

    first = await manager.on_bar(bar_closing_at(CUTOFF), open_position())
    second = await manager.on_bar(bar_closing_at(CUTOFF + timedelta(minutes=1)), open_position())

    assert first is not None
    assert second is None


async def test_flatten_rearms_on_the_next_session(tmp_path: Path) -> None:
    manager = SessionManager(AlwaysStrategy(), calendar(), journal(tmp_path))
    await manager.on_bar(bar_closing_at(CUTOFF), open_position())

    next_day_cutoff = CUTOFF + timedelta(days=1)
    signal = await manager.on_bar(bar_closing_at(next_day_cutoff), open_position())
    assert signal is not None


async def test_end_session_closes_the_open_session(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    manager = SessionManager(strategy, calendar(), journal(tmp_path))
    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)

    await manager.end_session()

    assert manager.current_session_date is None
    assert '"event": "session_end"' in (tmp_path / "2026-07-15.jsonl").read_text()


async def test_strategy_lifecycle_hooks_fire(tmp_path: Path) -> None:
    strategy = AlwaysStrategy()
    manager = SessionManager(strategy, calendar(), journal(tmp_path))

    await manager.on_bar(bar_closing_at(OPEN + timedelta(minutes=1)), None)
    strategy.restore_state({"fired": True})
    await manager.on_bar(bar_closing_at(OPEN + timedelta(days=1, minutes=1)), None)

    # on_session_start reset the flag, so the strategy is armed again.
    assert strategy.get_state() == {"fired": False}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.session'`

- [ ] **Step 3: Write `session.py`**

```python
from __future__ import annotations

from datetime import date

from nq_agent.clock import SessionCalendar
from nq_agent.journal import Journal
from nq_agent.models import Bar, Position, Signal, SignalIntent
from nq_agent.strategy.base import Strategy


class SessionManager:
    """Owns session lifecycle and the cutoff flatten.

    This is the only component besides the strategy allowed to generate a
    signal. Flatten is generation, not veto, so it cannot live in the risk
    layer without breaking that layer's one invariant.
    """

    def __init__(self, strategy: Strategy, calendar: SessionCalendar, journal: Journal) -> None:
        self._strategy = strategy
        self._calendar = calendar
        self._journal = journal
        self._session_date: date | None = None
        self._flattened_for: date | None = None

    @property
    def current_session_date(self) -> date | None:
        return self._session_date

    async def _start(self, session_date: date) -> None:
        self._session_date = session_date
        self._strategy.on_session_start(session_date)
        await self._journal.write(
            "session_start", session_date, strategy=self._strategy.name
        )

    async def end_session(self) -> None:
        if self._session_date is None:
            return
        finished = self._session_date
        self._strategy.on_session_end(finished)
        await self._journal.write("session_end", finished, strategy=self._strategy.name)
        self._session_date = None

    async def on_bar(self, bar: Bar, position: Position | None) -> Signal | None:
        session_date = self._calendar.session_date_for(bar.close_time)

        if self._session_date != session_date:
            await self.end_session()
            await self._start(session_date)

        if position is None:
            return None
        if self._flattened_for == session_date:
            return None
        if bar.close_time < self._calendar.cutoff_utc(session_date):
            return None

        self._flattened_for = session_date
        signal = Signal(
            timestamp=bar.close_time,
            symbol=position.symbol,
            intent=SignalIntent.FLATTEN,
            direction=position.direction,
            quantity=position.quantity,
            reason="session cutoff reached with an open position",
        )
        await self._journal.write(
            "signal_emitted",
            session_date,
            signal_id=signal.id,
            source="session_manager",
            intent=signal.intent,
            direction=signal.direction,
            quantity=signal.quantity,
            reason=signal.reason,
        )
        return signal
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_session.py -v`
Expected: 9 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/session.py tests/test_session.py
git commit -m "feat: session manager owning lifecycle and cutoff flatten"
```

---

### Task 13: State persistence

**Files:**
- Create: `src/nq_agent/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `SessionState`
- Produces: `StateStore(db_path: Path)` with `async init_schema() -> None`, `async save(state: SessionState) -> None`, `async load(session_date: date) -> SessionState | None`.

- [ ] **Step 1: Write the failing test**

`tests/test_state.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from nq_agent.models import Direction, Position, SessionState
from nq_agent.state import StateStore


def position() -> Position:
    return Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=1,
        entry_price=Decimal("20100.25"),
        entry_time=datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),
        stop_price=Decimal("20090.25"),
        target_price=Decimal("20120.25"),
    )


async def test_load_returns_none_when_nothing_saved(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()
    assert await store.load(date(2026, 7, 15)) is None


async def test_save_then_load_round_trips_every_field(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()

    state = SessionState(
        session_date=date(2026, 7, 15),
        trades_taken=2,
        is_halted=True,
        strategy_state={"fired": True, "count": 3},
        last_bar_time=datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc),
        position=position(),
    )
    await store.save(state)

    loaded = await store.load(date(2026, 7, 15))
    assert loaded == state
    assert loaded is not None
    assert loaded.position is not None
    assert loaded.position.entry_price == Decimal("20100.25")


async def test_save_overwrites_the_same_session_date(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()

    await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=1))
    await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=2))

    loaded = await store.load(date(2026, 7, 15))
    assert loaded is not None
    assert loaded.trades_taken == 2


async def test_sessions_are_isolated_by_date(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()

    await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=1))
    await store.save(SessionState(session_date=date(2026, 7, 16), trades_taken=9))

    first = await store.load(date(2026, 7, 15))
    second = await store.load(date(2026, 7, 16))
    assert first is not None and first.trades_taken == 1
    assert second is not None and second.trades_taken == 9


async def test_init_schema_creates_the_parent_directory(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "nested" / "state.db")
    await store.init_schema()
    assert (tmp_path / "nested" / "state.db").exists()


async def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    await store.init_schema()
    await store.save(SessionState(session_date=date(2026, 7, 15), trades_taken=4))
    await store.init_schema()

    loaded = await store.load(date(2026, 7, 15))
    assert loaded is not None
    assert loaded.trades_taken == 4
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.state'`

- [ ] **Step 3: Write `state.py`**

```python
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from pathlib import Path

from nq_agent.models import SessionState

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_state (
    session_date TEXT PRIMARY KEY,
    payload      TEXT NOT NULL
)
"""


class StateStore:
    """SessionState persistence, written after every state transition.

    SQLite through the stdlib driver on a worker thread. Two writes a day does
    not justify an aiosqlite dependency, and a failed write is re-raised rather
    than swallowed — silent state corruption is worse than a crash.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _init_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(SCHEMA)

    def _save_sync(self, session_date: str, payload: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO session_state (session_date, payload) VALUES (?, ?) "
                "ON CONFLICT(session_date) DO UPDATE SET payload = excluded.payload",
                (session_date, payload),
            )

    def _load_sync(self, session_date: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM session_state WHERE session_date = ?", (session_date,)
            ).fetchone()
        return str(row[0]) if row else None

    async def init_schema(self) -> None:
        await asyncio.to_thread(self._init_sync)

    async def save(self, state: SessionState) -> None:
        await asyncio.to_thread(
            self._save_sync, state.session_date.isoformat(), state.model_dump_json()
        )

    async def load(self, session_date: date) -> SessionState | None:
        payload = await asyncio.to_thread(self._load_sync, session_date.isoformat())
        if payload is None:
            return None
        return SessionState.model_validate_json(payload)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: 6 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/state.py tests/test_state.py
git commit -m "feat: SQLite session state persistence"
```

---

### Task 14: Engine wiring, CLI, and the end-to-end run

**Files:**
- Create: `src/nq_agent/main.py`
- Create: `src/nq_agent/__main__.py`
- Test: `tests/test_end_to_end.py`

**Interfaces:**
- Consumes: everything built so far
- Produces: `build_executors(settings) -> list[Executor]`, `build_strategy(name) -> Strategy`, `Engine(...)` with `async run() -> None` and public attributes `strategy`, `trades_taken`, `is_halted`; `async run_from_config(config_path, replay_path, strategy_name, max_bars, strategy_override: Strategy | None = None) -> Engine`; `main() -> None`.

`strategy_override` exists so tests can inject a strategy that the CLI has no name for. Production paths always go through `build_strategy`.

- [ ] **Step 1: Write the failing test**

`tests/test_end_to_end.py`:

```python
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nq_agent.config import load_settings
from nq_agent.context import Context
from nq_agent.main import run_from_config
from nq_agent.models import Bar, Direction, Signal, SignalIntent
from nq_agent.state import StateStore
from nq_agent.strategy.base import Strategy

FIXTURE = Path("tests/fixtures/2026-07-15.jsonl")
SESSION = date(2026, 7, 15)


class EveryBarStrategy(Strategy):
    """Fires on every 1m bar. Exists to make warmup suppression observable."""

    name = "every_bar"
    required_timeframes = ["1m"]

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        if bar.timeframe != "1m":
            return None
        return Signal(
            timestamp=bar.close_time,
            symbol=bar.symbol,
            direction=Direction.LONG,
            entry_price=bar.close,
            stop_price=bar.close - Decimal("10"),
            target_price=bar.close + Decimal("20"),
            quantity=1,
            reason="every bar",
        )

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {}

    def restore_state(self, state: dict[str, Any]) -> None:
        return None


class ExplodingStrategy(Strategy):
    """Raises on the third bar. Exists to prove the engine halts rather than dies."""

    name = "exploding"
    required_timeframes = ["1m"]

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, bar: Bar, context: Context) -> Signal | None:
        self.calls += 1
        if self.calls == 3:
            raise RuntimeError("strategy exploded")
        return None

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {"calls": self.calls}

    def restore_state(self, state: dict[str, Any]) -> None:
        self.calls = int(state.get("calls", 0))


def settings_for(tmp_path: Path) -> Path:
    """Write a paper config whose data_dir points at tmp_path."""
    config = tmp_path / "test.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify, mff, fundednext]\n"
        "  - name: dryrun_notify\n"
        "    type: notify\n"
        "    enabled: true\n"
        "    accounts: []\n"
    )
    return config


def journal_events(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "journal" / "2026-07-15.jsonl"
    return [json.loads(line) for line in path.read_text().strip().splitlines()]


async def test_stub_strategy_runs_a_full_session_without_trading(tmp_path: Path) -> None:
    engine = await run_from_config(settings_for(tmp_path), FIXTURE, "stub", None)

    events = {event["event"] for event in journal_events(tmp_path)}
    assert "session_start" in events
    assert "session_end" in events
    assert "signal_emitted" not in events
    assert engine.trades_taken == 0


async def test_always_strategy_reaches_every_executor(tmp_path: Path) -> None:
    engine = await run_from_config(settings_for(tmp_path), FIXTURE, "always", None)

    results = [e for e in journal_events(tmp_path) if e["event"] == "order_result"]
    names = {str(e["executor_name"]) for e in results}
    assert names == {
        "dryrun_notify",
        "dryrun_broker:tradeify",
        "dryrun_broker:mff",
        "dryrun_broker:fundednext",
    }
    assert all(e["success"] is True for e in results)
    assert engine.trades_taken == 1


async def test_notify_result_is_journaled_before_the_broker_results(tmp_path: Path) -> None:
    await run_from_config(settings_for(tmp_path), FIXTURE, "always", None)

    results = [e for e in journal_events(tmp_path) if e["event"] == "order_result"]
    assert str(results[0]["executor_name"]) == "dryrun_notify"


async def test_signal_carries_absolute_prices(tmp_path: Path) -> None:
    await run_from_config(settings_for(tmp_path), FIXTURE, "always", None)

    emitted = next(e for e in journal_events(tmp_path) if e["event"] == "signal_emitted")
    assert emitted["intent"] == SignalIntent.ENTRY.value
    assert "entry_price" in emitted
    assert "stop_price" in emitted
    assert "target_price" in emitted


async def test_kill_switch_vetoes_the_entry(tmp_path: Path) -> None:
    config = settings_for(tmp_path)
    (tmp_path / "nq-agent.halt").write_text("halt")

    engine = await run_from_config(config, FIXTURE, "always", None)

    vetoes = [e for e in journal_events(tmp_path) if e["event"] == "risk_veto"]
    assert vetoes
    assert vetoes[0]["reason"] == "KILL_SWITCH"
    assert engine.trades_taken == 0


async def test_state_is_persisted_at_the_end_of_the_run(tmp_path: Path) -> None:
    config = settings_for(tmp_path)
    await run_from_config(config, FIXTURE, "always", None)

    store = StateStore(load_settings(config).state_db_path)
    await store.init_schema()
    state = await store.load(SESSION)

    assert state is not None
    assert state.trades_taken == 1
    assert state.strategy_state == {"fired": True}
    assert state.last_bar_time is not None


async def test_restart_resumes_without_refiring_the_entry(tmp_path: Path) -> None:
    config = settings_for(tmp_path)

    first = await run_from_config(config, FIXTURE, "always", max_bars=60)
    assert first.trades_taken == 1

    second = await run_from_config(config, FIXTURE, "always", max_bars=None)

    entries = [
        e
        for e in journal_events(tmp_path)
        if e["event"] == "signal_emitted" and e["intent"] == SignalIntent.ENTRY.value
    ]
    assert len(entries) == 1, "the morning's entry must not fire twice"
    assert second.trades_taken == 1


async def test_restart_rebuilds_strategy_state(tmp_path: Path) -> None:
    config = settings_for(tmp_path)
    await run_from_config(config, FIXTURE, "always", max_bars=60)
    engine = await run_from_config(config, FIXTURE, "always", max_bars=None)
    assert engine.strategy.get_state() == {"fired": True}


async def test_backfill_signals_are_suppressed_and_journaled(tmp_path: Path) -> None:
    config = settings_for(tmp_path)

    await run_from_config(
        config, FIXTURE, "always", max_bars=10, strategy_override=EveryBarStrategy()
    )
    await run_from_config(
        config, FIXTURE, "always", max_bars=20, strategy_override=EveryBarStrategy()
    )

    suppressed = [
        e for e in journal_events(tmp_path) if e["event"] == "signal_suppressed_backfill"
    ]
    assert suppressed, "backfill replay must journal every signal it swallowed"


async def test_backfill_never_reaches_an_executor(tmp_path: Path) -> None:
    config = settings_for(tmp_path)

    await run_from_config(
        config, FIXTURE, "always", max_bars=10, strategy_override=EveryBarStrategy()
    )
    before = len([e for e in journal_events(tmp_path) if e["event"] == "order_result"])

    await run_from_config(
        config, FIXTURE, "always", max_bars=10, strategy_override=EveryBarStrategy()
    )
    after = len([e for e in journal_events(tmp_path) if e["event"] == "order_result"])

    assert after == before, "a replayed bar must not produce a new order"


async def test_a_raising_strategy_halts_the_session_instead_of_crashing(
    tmp_path: Path,
) -> None:
    engine = await run_from_config(
        settings_for(tmp_path), FIXTURE, "stub", None, strategy_override=ExplodingStrategy()
    )

    events = journal_events(tmp_path)
    assert any(e["event"] == "strategy_error" for e in events)
    assert any(e["event"] == "session_end" for e in events)
    assert engine.is_halted is True


async def test_unknown_strategy_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        await run_from_config(settings_for(tmp_path), FIXTURE, "nonexistent", None)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_end_to_end.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nq_agent.main'`

- [ ] **Step 3: Write `main.py`**

```python
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from datetime import date, datetime

from nq_agent.clock import SessionCalendar, SimClock
from nq_agent.config import Settings, load_settings
from nq_agent.context import Context
from nq_agent.execution.base import Executor
from nq_agent.execution.dryrun import DryRunExecutor, DryRunNotifier
from nq_agent.feed.base import DataFeed
from nq_agent.feed.replay import ReplayFeed
from nq_agent.journal import Journal
from nq_agent.models import Bar, Position, SessionState, Signal, SignalIntent
from nq_agent.position import PositionTracker
from nq_agent.risk.accounts import AccountRegistry
from nq_agent.risk.limits import RiskManager
from nq_agent.router import Router
from nq_agent.session import SessionManager
from nq_agent.state import StateStore
from nq_agent.strategy.always import AlwaysStrategy
from nq_agent.strategy.base import Strategy
from nq_agent.strategy.stub import StubStrategy

logger = logging.getLogger(__name__)

STRATEGIES: dict[str, type[Strategy]] = {"stub": StubStrategy, "always": AlwaysStrategy}


def build_strategy(name: str) -> Strategy:
    try:
        return STRATEGIES[name]()
    except KeyError:
        known = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"unknown strategy '{name}'; known strategies: {known}") from None


def build_executors(settings: Settings) -> list[Executor]:
    """Expand each config entry into one executor instance per account.

    A notify entry has no accounts and becomes a single instance. Webhook
    entries are not built here — they arrive with the Databento plan.
    """
    executors: list[Executor] = []
    for entry in settings.executors:
        if not entry.enabled:
            continue
        if entry.type == "notify":
            executors.append(DryRunNotifier(entry.name))
        elif entry.type == "dryrun":
            for account in entry.accounts or [None]:
                executors.append(DryRunExecutor(entry.name, account_id=account))
        else:
            raise ValueError(f"executor type '{entry.type}' is not available in this build")
    return executors


class Engine:
    """The bar loop. Everything else is a collaborator."""

    def __init__(
        self,
        settings: Settings,
        feed: DataFeed,
        strategy: Strategy,
        clock: SimClock,
        calendar: SessionCalendar,
        journal: Journal,
        router: Router,
        risk: RiskManager,
        state_store: StateStore,
        resume_from: datetime | None = None,
        resume_position: Position | None = None,
        trades_taken: int = 0,
        max_bars: int | None = None,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self.trades_taken = trades_taken
        self.is_halted = False
        self._feed = feed
        self._calendar = calendar
        self._journal = journal
        self._router = router
        self._risk = risk
        self._state = state_store
        self._resume_from = resume_from
        self._max_bars = max_bars
        self._tracker = PositionTracker()
        self._tracker.restore(resume_position)
        self._session = SessionManager(strategy, calendar, journal)
        self._context = Context(clock, calendar, settings.context.history_bars)
        self._last_bar_time: datetime | None = None

    async def _persist(self) -> None:
        session_date = self._session.current_session_date
        if session_date is None:
            return
        await self._state.save(
            SessionState(
                session_date=session_date,
                trades_taken=self.trades_taken,
                is_halted=self.is_halted,
                strategy_state=self.strategy.get_state(),
                last_bar_time=self._last_bar_time,
                position=self._tracker.position,
            )
        )

    async def _handle_signal(self, signal: Signal, bar: Bar, warmup: bool) -> None:
        session_date = self._session.current_session_date
        assert session_date is not None

        if warmup:
            await self._journal.write(
                "signal_suppressed_backfill",
                session_date,
                signal_id=signal.id,
                intent=signal.intent,
                reason=signal.reason,
            )
            return

        veto = self._risk.check(signal, self.trades_taken, len(self._router.enabled))
        if veto is not None:
            await self._journal.write(
                "risk_veto",
                session_date,
                signal_id=veto.signal_id,
                reason=veto.reason,
                detail=veto.detail,
            )
            return

        await self._journal.write(
            "signal_emitted",
            session_date,
            signal_id=signal.id,
            source="strategy" if signal.intent is SignalIntent.ENTRY else "session_manager",
            intent=signal.intent,
            direction=signal.direction,
            quantity=signal.quantity,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            reason=signal.reason,
        )

        await self._router.dispatch(signal, session_date)
        self._risk.record_accepted(signal)

        if signal.intent is SignalIntent.ENTRY:
            self._tracker.on_signal(signal)
            self.trades_taken += 1
            await self._journal.write(
                "position_opened",
                session_date,
                signal_id=signal.id,
                entry_price=signal.entry_price,
            )
        else:
            closed = self._tracker.flatten(bar.close, bar.close_time)
            if closed is not None:
                await self._journal.write(
                    "position_closed",
                    session_date,
                    exit_price=closed.exit_price,
                    exit_reason=closed.exit_reason,
                )

    async def _run_strategy(self, bar: Bar, session_date: date) -> Signal | None:
        """Call the strategy, converting a raise into a halted session.

        A strategy bug must not take the process down mid-position. The session
        stops calling on_bar, but the loop keeps running so the cutoff flatten
        and session_end still happen.
        """
        if self.is_halted:
            return None
        try:
            return self.strategy.on_bar(bar, self._context)
        except Exception as exc:  # noqa: BLE001 - a strategy bug halts, never crashes
            self.is_halted = True
            logger.exception("strategy %s raised", self.strategy.name)
            await self._journal.write(
                "strategy_error",
                session_date,
                strategy=self.strategy.name,
                error=f"{type(exc).__name__}: {exc}",
                bar_close_time=bar.close_time,
            )
            return None

    async def run(self) -> None:
        await self._state.init_schema()
        for executor in self._router.enabled:
            if not await executor.health_check():
                logger.warning("health check failed for %s", executor.name)

        processed = 0
        async for bar in self._feed.stream(
            self.settings.symbol, self.settings.timeframes, self._resume_from
        ):
            warmup = self._resume_from is not None and bar.close_time <= self._resume_from
            self._context.set_warmup(warmup)
            self._last_bar_time = bar.close_time

            closed = self._tracker.on_bar(bar)
            flatten_signal = await self._session.on_bar(bar, self._tracker.position)
            session_date = self._session.current_session_date
            assert session_date is not None

            if closed is not None:
                await self._journal.write(
                    "position_closed",
                    session_date,
                    exit_price=closed.exit_price,
                    exit_reason=closed.exit_reason,
                )

            self._context.record_bar(bar)
            self._context.set_position(self._tracker.position)
            self._context.set_trades_taken(self.trades_taken)

            if flatten_signal is not None:
                await self._handle_signal(flatten_signal, bar, warmup)

            strategy_signal = await self._run_strategy(bar, session_date)
            if strategy_signal is not None:
                await self._handle_signal(strategy_signal, bar, warmup)

            if not warmup:
                await self._persist()

            processed += 1
            if self._max_bars is not None and processed >= self._max_bars:
                break

        await self._session.end_session()
        await self._feed.close()


async def run_from_config(
    config_path: Path,
    replay_path: Path,
    strategy_name: str,
    max_bars: int | None,
    strategy_override: Strategy | None = None,
) -> Engine:
    settings = load_settings(config_path)
    strategy = strategy_override or build_strategy(strategy_name)

    calendar = SessionCalendar(
        settings.session.timezone, settings.session.open, settings.session.cutoff
    )

    state_store = StateStore(settings.state_db_path)
    await state_store.init_schema()

    first_tick = ReplayFeed(replay_path, settings.symbol).first_tick_time()
    session_date = calendar.session_date_for(first_tick)
    prior = await state_store.load(session_date)

    resume_from: datetime | None = None
    resume_position: Position | None = None
    trades_taken = 0
    if prior is not None:
        strategy.restore_state(prior.strategy_state)
        resume_from = prior.last_bar_time
        resume_position = prior.position
        trades_taken = prior.trades_taken

    clock = SimClock(first_tick)
    journal = Journal(settings.journal_dir, clock)
    feed = ReplayFeed(replay_path, settings.symbol, clock=clock)

    assert settings.risk.kill_switch_path is not None
    risk = RiskManager(
        calendar=calendar,
        max_trades_per_day=settings.risk.max_trades_per_day,
        duplicate_window_seconds=settings.risk.duplicate_window_seconds,
        kill_switch_path=settings.risk.kill_switch_path,
    )
    registry = AccountRegistry(settings.data_dir / "accounts.yaml")
    router = Router(
        build_executors(settings),
        journal,
        settings.router.executor_timeout_seconds,
        settings.router.notify_timeout_seconds,
        settings.router.partial_fan,
        enabled_accounts=registry.enabled_accounts,
    )

    engine = Engine(
        settings=settings,
        feed=feed,
        strategy=strategy,
        clock=clock,
        calendar=calendar,
        journal=journal,
        router=router,
        risk=risk,
        state_store=state_store,
        resume_from=resume_from,
        resume_position=resume_position,
        trades_taken=trades_taken,
        max_bars=max_bars,
    )
    await engine.run()
    return engine


def main() -> None:
    parser = argparse.ArgumentParser(prog="nq_agent")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--strategy", default="always", choices=sorted(STRATEGIES))
    parser.add_argument(
        "--max-bars",
        type=int,
        default=None,
        help="stop after N bars; used to simulate a mid-session kill",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_from_config(args.config, args.replay, args.strategy, args.max_bars))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Write `__main__.py`**

```python
from nq_agent.main import main

main()
```

- [ ] **Step 5: Run the end-to-end tests**

Run: `uv run pytest tests/test_end_to_end.py -v`
Expected: 12 passed

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: all tests pass across every file

- [ ] **Step 7: Run the definition-of-done command**

```bash
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl
```

Expected: the process runs a full simulated session and exits 0. Then confirm the artefacts:

```bash
cat var/journal/2026-07-15.jsonl | head -5
sqlite3 var/state.db "SELECT session_date, payload FROM session_state;"
```

Expected: journal contains `session_start`, `signal_emitted`, four `order_result` records, and `session_end`. The state row shows `trades_taken` of 1.

- [ ] **Step 8: Verify the mid-session kill and restart by hand**

```bash
rm -rf var
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl --max-bars 60
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl
grep -c '"intent": "ENTRY"' var/journal/2026-07-15.jsonl
```

Expected: `1`. The morning survived the restart and the entry did not fire twice.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check . && uv run mypy src/nq_agent
git add src/nq_agent/main.py src/nq_agent/__main__.py tests/test_end_to_end.py
git commit -m "feat: engine wiring, CLI, and warmup-safe crash recovery"
```

---

## Definition of Done

```bash
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl
```

runs a full simulated session, emits a signal from `AlwaysStrategy`, routes it to three dry-run executors and a dry-run notifier, writes a complete journal, survives a mid-session kill and restart with state intact — and `strategy/stub.py` is a 20-line stub.

Adding the real strategy is then implementing one class.

## Follow-up plan (not this plan)

- `DatabentoFeed` against the same `DataFeed` ABC, including `resume_from` backfill
- Feed disconnect handling: exponential backoff reconnect, `feed_error` journaling, re-entering warmup on reconnect. Deferred because `ReplayFeed` cannot disconnect — there is nothing to test against until a live feed exists
- Malformed tick rejection with a `bar_gap` counter. Deferred for the same reason: fixture ticks are generated, not received
- Real `WebhookExecutor` for Signal Trade App
- Concrete `NotifyExecutor` once ntfy or Pushover is chosen
- Replace the synthetic fixture with 5–10 recorded trading days

## Spec coverage

Every section of the design doc maps to a task. The three deferred items above are the only gaps, each with a stated reason and a home in the follow-up plan.

| Spec section | Task |
|---|---|
| Decisions, Config | 1 |
| Core models | 2 |
| Clock and sessions | 3 |
| Journal | 4 |
| Aggregator, ordering, gaps | 5 |
| `DataFeed`, warmup contract | 6 |
| `Strategy`, `Context` | 7 |
| `Executor`, notify base | 8 |
| Router, partial fan | 9 |
| Risk layer, kill switch, accounts | 10 |
| `PositionTracker`, stop-wins | 11 |
| `SessionManager`, cutoff flatten | 12 |
| State and recovery | 13 |
| Error handling, definition of done | 14 |

