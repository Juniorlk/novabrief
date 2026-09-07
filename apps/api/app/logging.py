"""Structured logging.

Logs are JSON and correlated by `debug_id` (ADR-07): one identifier follows a
meeting from the desktop client through the API, the workers and the provider
calls, so a support request resolves with a search instead of an excavation.

Two rules are enforced here rather than left to reviewers:

* the `debug_id` is bound to the context, so every log line inside a request
  carries it without anyone having to remember to pass it;
* meeting content never reaches the logs. There is no transcript, no summary
  and no participant name in any log line, in any environment.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.typing import EventDict, WrappedLogger

from app.config import Settings

# Keys that must never appear in a log line, whatever the caller intended.
# Meeting content is the product's most sensitive asset (§21.1) and a log
# pipeline is the easiest place to leak it by accident.
_FORBIDDEN_KEYS = frozenset(
    {
        "transcript",
        "raw_text",
        "summary",
        "text",
        "content",
        "audio",
        "password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "totp_secret",
        "authorization",
    }
)

_REDACTED = "[redacted]"


def drop_sensitive_values(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Redact anything that could carry meeting content or a credential.

    Redacting rather than dropping keeps the shape of the log line, so it stays
    obvious in review that a field was passed and suppressed.
    """
    for key in list(event_dict):
        if key.lower() in _FORBIDDEN_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Install the logging pipeline for the process."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        drop_sensitive_values,
    ]

    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if settings.log_format == "console"
        else structlog.processors.JSONRenderer()
    )

    # Uvicorn and SQLAlchemy log through the standard library, so structlog is
    # routed through it too: one pipeline, one format, one stream. Using
    # structlog's own PrintLogger here would split the output in two and break
    # `add_logger_name`, which needs a stdlib logger to read a name from.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_request_context(*, debug_id: str, **extra: object) -> None:
    """Bind identifiers for the duration of the current request or task."""
    bind_contextvars(debug_id=debug_id, **extra)


def clear_request_context() -> None:
    """Drop the bound identifiers once the request or task is finished."""
    clear_contextvars()


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a logger that carries the bound context."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
