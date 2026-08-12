from __future__ import annotations

import typing
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_CONFIG = Path("config/base.yaml")


class SessionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    timezone: str = "America/New_York"
    open: time = time(9, 30)
    cutoff: time = time(16, 30)


class ContextConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    history_bars: int = 500


class FeedConfig(BaseModel):
    """Reconnect policy for a live feed. See feed/reconnecting.py."""

    model_config = ConfigDict(frozen=True)

    max_reconnect_attempts: int = 5
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0


class ContractConfig(BaseModel):
    """What one point of price movement is worth.

    Nothing in the trading path uses this -- signals carry absolute prices and
    the executors convert. It exists so P&L can be expressed in money, which
    means every number the backtest report prints depends on it being right
    for the instrument actually being traded. NQ and MNQ differ by 10x here,
    so a wrong value does not look wrong, it just scales every result.
    """

    model_config = ConfigDict(frozen=True)

    point_value: Decimal = Decimal("20")  # NQ = $20/point, MNQ = $2/point
    tick_size: Decimal = Decimal("0.25")
    # Round turn: commission plus exchange and clearing fees, both sides.
    # Defaults to zero because the real figure is broker-specific, and a
    # backtest silently netting a made-up cost is worse than one that reports
    # its costs as zero and says so.
    commission_per_round_turn: Decimal = Decimal("0")


class RiskConfig(BaseModel):
    """Deliberately the one unfrozen config model.

    `Settings.resolve_derived_paths` assigns to `self.risk.kill_switch_path`
    after construction, so this model cannot be frozen like its siblings.
    """

    max_trades_per_day: int = 2
    duplicate_window_seconds: int = 60
    kill_switch_path: Path | None = None
    # Both in account currency, both magnitudes ("500" = stop after losing
    # 500), both opt-in. null means the limit is not enforced -- which is the
    # right default for a replay or a backtest and the wrong one for a funded
    # account, so live.yaml sets them.
    max_daily_loss: Decimal | None = None
    max_trailing_drawdown: Decimal | None = None
    # "equity" trails the high-water mark of realised + open P&L, so an
    # unrealised peak moves the mark and giving it back is a drawdown.
    # "closed" keeps the high-water mark on realised balance only -- an
    # unrealised peak never raises it -- but an open LOSS still counts
    # against the distance to the mark on both bases. That asymmetry is
    # deliberate (see test_risk_unrealised): it can flatten earlier than a
    # firm that marks strictly end-of-day, and early is the survivable
    # direction. Prop firms genuinely differ here, and picking the wrong one
    # means the agent's limit and the firm's limit are measuring different
    # things. "equity" is the conservative reading: it trips first.
    trailing_drawdown_basis: Literal["equity", "closed"] = "equity"
    # Reconcile against the broker every N bars, on top of the startup and
    # post-UNKNOWN passes which always run when a position source is wired.
    # 0 disables the interval pass. It costs an API call each time and is the
    # only thing that catches a broker-side stop fill the agent never saw.
    reconcile_interval_bars: int = 0


class RouterConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    partial_fan: Literal["continue", "alert_only"] = "continue"
    executor_timeout_seconds: float = 5.0
    notify_timeout_seconds: float = 3.0


class ExecutorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

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
        frozen=True,
    )

    symbol: str = "NQ"
    timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m"])
    data_dir: Path = Path("./var")
    session: SessionConfig = SessionConfig()
    contract: ContractConfig = ContractConfig()
    context: ContextConfig = ContextConfig()
    feed: FeedConfig = FeedConfig()

    # Provider symbology, not the internal `symbol` above. ".c.0" is
    # Databento's continuous front-month: NQ rolls quarterly, and a hardcoded
    # dated contract silently stops producing data after expiry, which looks
    # exactly like a quiet market.
    databento_symbol: str = "NQ.c.0"
    databento_dataset: str = "GLBX.MDP3"
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


def _as_model(annotation: Any) -> type[BaseModel] | None:
    """The BaseModel a field's annotation names, if it names one directly.

    Unwraps nothing else -- `RiskConfig` matches, `list[RiskConfig]` and
    `RiskConfig | None` do not (those are handled by the two call sites in
    _validate_known_keys, which know whether they are looking at a mapping
    or a list of them).
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _validate_known_keys(data: dict[str, Any], model: type[BaseModel], section: str) -> None:
    """Reject a YAML key that does not correspond to a real field on `model`.

    Settings uses extra="ignore" so pydantic-settings can layer environment
    variables over YAML without every legacy env var needing a matching
    field -- flipping that would break the env-var path this exists to
    protect. But the same setting silently swallows a YAML typo: a
    misspelled max_trades_per_day would just fall back to the default limit
    with no error, on a value that is a risk control, not a preference. This
    walks the merged YAML tree instead, where a typo has no legitimate
    reason to exist, and is called before Settings(**data) ever sees it.
    """
    fields = model.model_fields
    for key, value in data.items():
        field = fields.get(key)
        if field is None:
            raise ValueError(f"unknown config key {key!r} in section {section!r}")

        nested = _as_model(field.annotation)
        if nested is not None and isinstance(value, dict):
            _validate_known_keys(value, nested, section=key)
            continue

        if typing.get_origin(field.annotation) is list and isinstance(value, list):
            args = typing.get_args(field.annotation)
            item_model = _as_model(args[0]) if args else None
            if item_model is not None:
                for item in value:
                    if isinstance(item, dict):
                        _validate_known_keys(item, item_model, section=f"{key}[]")


def load_settings(config_path: Path) -> Settings:
    """Layer base.yaml under the given config, then let env vars supply secrets.

    YAML carries structure. Environment carries secrets. They never overlap,
    so there is no precedence conflict between the two layers.
    """
    data = _read_yaml(BASE_CONFIG)
    if config_path.resolve() != BASE_CONFIG.resolve():
        data = _deep_merge(data, _read_yaml(config_path))
    _validate_known_keys(data, Settings, section="<top level>")
    return Settings(**data)
