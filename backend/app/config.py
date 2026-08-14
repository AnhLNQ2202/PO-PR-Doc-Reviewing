from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_MODEL = "GreenNode/GreenMind-Medium-14B-R1"


class Settings(BaseSettings):
    """Runtime configuration loaded exclusively from environment variables."""

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    maas_api_key: SecretStr | None = None
    maas_base_url: str | None = None
    allow_insecure_maas_http: bool = False
    maas_model: str = DEFAULT_MODEL
    maas_timeout_seconds: float = Field(default=240.0, gt=0)
    maas_max_output_tokens: int = Field(default=8_000, gt=0)
    maas_chunk_chars: int = Field(default=16_000, ge=2_000)
    maas_merge_chars: int = Field(default=60_000, ge=8_000)
    maas_json_mode: Literal["none", "json_object"] = "none"

    ocr_lang: str = "vie+eng"
    temp_dir: Path = Path(tempfile.gettempdir()) / "po-pr-reviewing"
    app_version: str = "dev"
    log_level: str = "INFO"

    # The supplied deployment runs one API replica. On startup, every leftover
    # request directory is from a dead process and must be removed immediately.
    # A future shared-volume multi-replica deployment must set a positive grace.
    stale_temp_seconds: int = Field(default=0, ge=0)

    @field_validator("maas_base_url")
    @classmethod
    def normalize_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        if not normalized:
            return None
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("MAAS_BASE_URL must start with http:// or https://")
        return normalized

    @model_validator(mode="after")
    def require_secure_maas_transport(self) -> "Settings":
        if (
            self.maas_base_url
            and self.maas_base_url.startswith("http://")
            and not self.allow_insecure_maas_http
        ):
            raise ValueError(
                "HTTP MAAS_BASE_URL requires explicit "
                "ALLOW_INSECURE_MAAS_HTTP=true opt-in"
            )
        return self

    @field_validator("maas_model", "ocr_lang", "app_version")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value

    @property
    def maas_configured(self) -> bool:
        return bool(self.maas_api_key and self.maas_base_url and self.maas_model)
