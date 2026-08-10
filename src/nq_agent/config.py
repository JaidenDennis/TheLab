from __future__ import annotations

from datetime import time
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


class RiskConfig(BaseModel):
    """Deliberately the one unfrozen config model.

    `Settings.resolve_derived_paths` assigns to `self.risk.kill_switch_path`
    after construction, so this model cannot be frozen like its siblings.
    """

    max_trades_per_day: int = 2
    duplicate_window_seconds: int = 60
    kill_switch_path: Path | None = None


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
