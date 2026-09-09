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

    # Celery falls back to the same Redis. Separate settings because a busy
    # deployment moves the queue to its own instance, and the API's cache
    # should not be evicted by a backlog of meetings.
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    # A meeting of an hour takes minutes; this is the ceiling past which a task
    # is assumed stuck rather than slow.
    celery_task_soft_time_limit_seconds: int = 1800
    # Section 22.3: 02:00 UTC, when nobody is recording.
    purge_cron_hour: int = 2
    purge_cron_minute: int = 0

    @property
    def broker_url(self) -> str:
        """Where the queue lives."""
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        """Where task results live."""
        return self.celery_result_backend or self.redis_url

    # RS256 key pair, PEM encoded. Asymmetric so workers can verify a token with
    # the public key alone while only the API can mint one (section 17.3).
    jwt_private_key: str | None = None
    jwt_public_key: str | None = None
    jwt_access_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    # Object storage. Cloudflare R2 in production, MinIO in development: both
    # speak the S3 API, so only these values change between the two (ADR-01).
    r2_account_id: str | None = None
    r2_endpoint: str = "http://localhost:9000"
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    # R2 has no regions and rejects anything else; MinIO ignores it.
    r2_region: str = "auto"
    r2_bucket_audio: str = "novabrief-audio"
    # Section 21.1: long enough to upload a part on a poor connection, short
    # enough that a URL found in a log is already dead.
    r2_presign_ttl_seconds: int = 900
    # S3 refuses parts under 5 MiB except the last one, so this is a floor
    # rather than a preference (section 16.4).
    r2_multipart_part_size_bytes: int = 5 * 1024 * 1024

    resend_api_key: str | None = None
    # The domain verified with the email provider. Sending from anything else
    # is refused by the provider, so this default has to be the real one.
    email_from: str = "NovaBrief <no-reply@novabrief.cloud>"
    email_reply_to: str | None = None

    rate_limit_auth_per_minute: int = 10
    rate_limit_api_per_minute: int = 600

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

    def require_storage_credentials(self) -> tuple[str, str]:
        """The object-storage key pair, or a clear failure.

        Falling back to anonymous access would look like it works against a
        permissive MinIO and fail only once it reached R2, which is the worst
        possible moment to find out.
        """
        if not self.r2_access_key_id or not self.r2_secret_access_key:
            message = (
                "R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY are not set; "
                "object storage cannot be reached."
            )
            raise RuntimeError(message)
        return self.r2_access_key_id, self.r2_secret_access_key

    def require_jwt_private_key(self) -> str:
        """The signing key, or a clear failure.

        Generating a throwaway key when none is configured would look like it
        works and silently invalidate every session on restart, and across
        replicas would mean tokens minted by one instance are rejected by the
        next. Better to refuse than to be subtly broken.
        """
        if not self.jwt_private_key:
            message = (
                "JWT_PRIVATE_KEY is not set. Generate an RS256 key pair and put "
                "it in the environment; the API will not mint tokens without one."
            )
            raise RuntimeError(message)
        return self.jwt_private_key

    def require_jwt_public_key(self) -> str:
        """The verification key, or a clear failure."""
        if not self.jwt_public_key:
            message = "JWT_PUBLIC_KEY is not set; access tokens cannot be verified."
            raise RuntimeError(message)
        return self.jwt_public_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings.

    Cached so that a request does not re-read the environment, and so tests can
    clear the cache to swap configuration.
    """
    return Settings()
