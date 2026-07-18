"""Environment-driven configuration.

Defaults follow the XDG Base Directory specification so that a fresh clone
runs unprivileged with no setup. Deployment overrides everything explicitly.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MediaKind = Literal["image", "video"]


def _xdg(env_var: str, fallback: str) -> Path:
    """Resolve an XDG base directory, falling back to a path under $HOME."""
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / fallback


def _default_media_root() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "higgshole" / "media"


def _default_db_path() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "higgshole" / "higgshole.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HIGGSHOLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openrouter_api_key: str | None = None
    openrouter_api_key_image: str | None = None
    openrouter_api_key_video: str | None = None

    media_root: Path = Field(default_factory=_default_media_root)
    db_path: Path = Field(default_factory=_default_db_path)

    bind_host: str = "127.0.0.1"
    bind_port: int = 8077

    daily_cap_usd: Decimal | None = None
    max_job_cost_usd: Decimal = Decimal("2.00")
    max_in_flight: int = 3

    job_timeout_minutes: int = 30
    poll_interval_seconds: int = 5
    max_retries: int = 3
    catalog_ttl_hours: int = 24

    reference_transport: str = "data_uri"

    @field_validator("media_root", "db_path")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()

    def openrouter_api_key_for(self, kind: MediaKind) -> str | None:
        """Return the key for a media kind, falling back to the shared default."""
        specific = {
            "image": self.openrouter_api_key_image,
            "video": self.openrouter_api_key_video,
        }[kind]
        return specific or self.openrouter_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
