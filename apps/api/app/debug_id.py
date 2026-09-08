"""Generation of the `debug_id` that follows work end to end (ADR-07).

The identifier is deliberately human-shaped: support reads it aloud on the
phone, a customer copies it out of the desktop app, and it is pasted into a
ticket. A bare UUID fails all three, which is why it exists alongside the
resource UUIDs rather than instead of them.

Form: ``DBG-<SCOPE>-<YYYYMMDD>-<RANDOM>``, for example
``DBG-API-20260907-8842``. The date narrows a log search to one day without
any index, and the scope says which subsystem minted it.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

# Digits only: the identifier is dictated over the phone, so anything that
# sounds alike (0/O, 1/I) or needs spelling out is a support cost.
_ALPHABET = "0123456789"
_RANDOM_LENGTH = 4


def new_debug_id(scope: str = "API", *, now: datetime | None = None) -> str:
    """Mint a debug identifier for the given scope."""
    moment = now or datetime.now(UTC)
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(_RANDOM_LENGTH))
    return f"DBG-{scope.upper()[:4]}-{moment:%Y%m%d}-{suffix}"
