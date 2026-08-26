"""Environment configuration. Secrets come from Render env vars only (spec §3)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Redis (Render Key Value)
    redis_url: str = "redis://localhost:6379/0"

    # Supabase — same service-role key desk-web uses
    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    agent_model: str = "claude-opus-5"
    classifier_model: str = "claude-haiku-4-5"

    # Tradovate (read-only API key; order permissions disabled at key level)
    tradovate_env: str = "demo"  # "demo" | "live"
    tradovate_username: str = ""
    tradovate_password: str = ""
    tradovate_cid: str = ""
    tradovate_secret: str = ""
    tradovate_device_id: str = "desk-brain"
    tradovate_app_id: str = "TradingDesk"
    tradovate_app_version: str = "0.1.0"

    # Databento (market data for the flow engine)
    databento_api_key: str = ""

    # Internal API (desk-web -> desk-brain, Render private network)
    brain_shared_secret: str = ""
    brain_host: str = "0.0.0.0"
    brain_port: int = 8321

    # Paths
    repo_root: Path = Path(__file__).resolve().parents[4]

    @property
    def factors_path(self) -> Path:
        return self.repo_root / "desk" / "factors.yaml"

    @property
    def signals_path(self) -> Path:
        return self.repo_root / "desk" / "signals.yaml"

    @property
    def voice_path(self) -> Path:
        return self.repo_root / "desk" / "voice.yaml"

    @property
    def tradovate_base(self) -> str:
        host = "live" if self.tradovate_env == "live" else "demo"
        return f"https://{host}.tradovateapi.com/v1"

    @property
    def tradovate_ws(self) -> str:
        host = "live" if self.tradovate_env == "live" else "demo"
        return f"wss://{host}.tradovateapi.com/v1/websocket"


@lru_cache
def settings() -> Settings:
    return Settings()
