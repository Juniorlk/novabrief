"""Application settings, read from the environment.

Every value comes from the environment (`.env` locally, injected secrets in
staging and production). Nothing here carries a price, a quota or a currency:
those live in the `plans` and `prices` tables so they can change without a
deployment (ADR-09).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["dev", "staging", "prod"]
LogFormat = Literal["json", "console"]


class Settings(BaseSettings):
    """Runtime configuration for the API and its workers."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = "dev"
    api_base_url: str = "http://localhost:8000"
    web_base_url: str = "http://localhost:3000"

    # Comma-separated in the environment, a list once parsed. NoDecode stops
    # pydantic-settings trying to JSON-decode it at the source, which happens
    # before any validator here can see the raw string.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    log_level: str = "info"
    log_format: LogFormat = "json"

    # The API connects with a restricted role. A SUPERUSER bypasses Row-Level
    # Security unconditionally, so connecting as the database owner would leave
    # every ADR-04 policy in place and enforcing nothing.
    database_url: str = (
        "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief"
    )
    # Separate credentials for migrations, which need DDL the API must not have.
    database_admin_url: str | None = None
    database_pool_size: int = 5
    database_max_overflow: int = 10

    redis_url: str = "redis://localhost:6379/0"

    sentry_dsn_api: str | None = None
    sentry_traces_sample_rate: float = 0.0

    default_market: str = "CM"
    default_locale: str = "fr"
    default_timezone: str = "Africa/Douala"

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept the comma-separated form the environment carries."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("log_format")
    @classmethod
    def _console_logs_stay_in_dev(cls, value: LogFormat, info: ValidationInfo) -> LogFormat:
        """Refuse human-readable logs outside dev.

        Staging and production logs are parsed and correlated by `debug_id`
        (ADR-07); a console format there would silently break that.
        """
        environment = info.data.get("environment", "dev")
        if value == "console" and environment != "dev":
            message = f"log_format 'console' is only allowed in dev, not in {environment}"
            raise ValueError(message)
        return value

    @property
    def is_production(self) -> bool:
        """True in production, where defaults must never be assumed."""
        return self.environment == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings.

    Cached so that a request does not re-read the environment, and so tests can
    clear the cache to swap configuration.
    """
    return Settings()
