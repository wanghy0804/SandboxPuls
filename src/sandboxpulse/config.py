"""Settings loaded from env / .env file."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hermes_target: str | None = Field(default=None, alias="SANDBOXPULSE_HERMES_TARGET")
    hermes_min_interval_s: float = Field(
        default=10.0, alias="SANDBOXPULSE_HERMES_MIN_INTERVAL_S"
    )
    hermes_debounce_s: float = Field(default=0.0, alias="SANDBOXPULSE_HERMES_DEBOUNCE_S")
    # gateway log to watch for inbound user messages (fresh context token
    # -> flush queued notifications immediately); empty disables the trigger
    hermes_pull_log: Path | None = Field(
        default=Path("~/.hermes/logs/gateway.log"), alias="SANDBOXPULSE_HERMES_PULL_LOG"
    )
    log_level: str = Field(default="INFO", alias="SANDBOXPULSE_LOG_LEVEL")
