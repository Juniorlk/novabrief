"""Meeting content and credentials must never reach a log line.

This is the guarantee section 21.1 makes to customers and section 22.3 makes to
operations. It is enforced by a processor rather than by reviewer discipline,
so it is tested like any other behaviour.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.logging import drop_sensitive_values


def _redact(**fields: object) -> dict[str, object]:
    """Run the processor the way structlog would."""
    return dict(drop_sensitive_values(None, "info", dict(fields)))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        "transcript",
        "raw_text",
        "summary",
        "text",
        "content",
        "password",
        "access_token",
        "refresh_token",
        "totp_secret",
        "authorization",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    result = _redact(**{key: "ce que le client a dit en réunion"})

    assert result[key] == "[redacted]"


def test_redaction_ignores_case() -> None:
    """A caller writing Authorization must not slip past the filter."""
    result = _redact(Authorization="Bearer secret-token")

    assert result["Authorization"] == "[redacted]"


def test_identifiers_survive_redaction() -> None:
    """Correlation must keep working: only content is removed, not context."""
    result = _redact(
        debug_id="DBG-API-20260907-4242",
        organization_id="0199c0ff-ee00-7000-8000-000000000001",
        meeting_id="0199c0ff-ee00-7000-8000-000000000002",
        duration_ms=12.5,
    )

    assert result["debug_id"] == "DBG-API-20260907-4242"
    assert result["duration_ms"] == 12.5
    assert "[redacted]" not in result.values()


def test_console_logs_are_refused_outside_dev() -> None:
    """Staging and production logs are parsed and correlated by debug_id.

    A human-readable format there would break that silently, so the
    configuration refuses it rather than accepting and degrading.
    """
    with pytest.raises(ValueError, match="only allowed in dev"):
        Settings(environment="prod", log_format="console")


def test_console_logs_are_allowed_in_dev() -> None:
    settings = Settings(environment="dev", log_format="console")

    assert settings.log_format == "console"
