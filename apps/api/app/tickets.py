"""Single-use tickets for the status WebSocket (section 17.2, EF-44).

A browser cannot set an Authorization header when it opens a WebSocket. The
usual workarounds are worse than the problem: a token in the query string ends
up in every access log and proxy trace, and a cookie brings CSRF with it.

So the client asks an authenticated endpoint for a ticket and opens the socket
with that. It lives sixty seconds, works once, and grants exactly one thing —
watching one meeting's status. Leaked, it is worth almost nothing, which is the
point.

Redis in production, in memory for tests and for a developer without one. Same
shape as the rate limiter: a protocol with two implementations, so no test
needs a server to run.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

import redis.asyncio as redis

from app.logging import get_logger

logger = get_logger(__name__)

TICKET_TTL_SECONDS = 60
_PREFIX = "ws-ticket:"


def new_ticket() -> str:
    """A ticket nobody can guess."""
    return f"wst_{secrets.token_urlsafe(32)}"


@dataclass(frozen=True)
class TicketClaims:
    """Who the ticket belongs to and what it may watch."""

    organization_id: uuid.UUID
    user_id: uuid.UUID
    meeting_id: uuid.UUID


class TicketStore(Protocol):
    """Issues and redeems single-use tickets."""

    async def issue(self, ticket: str, claims: TicketClaims) -> None:
        """Store a ticket for its short life."""
        ...

    async def redeem(self, ticket: str) -> TicketClaims | None:
        """Consume a ticket, returning its claims once and only once."""
        ...


def _encode(claims: TicketClaims) -> str:
    return f"{claims.organization_id}:{claims.user_id}:{claims.meeting_id}"


def _decode(raw: str) -> TicketClaims | None:
    parts = raw.split(":")
    if len(parts) != 3:  # pragma: no cover - only a corrupted entry
        return None
    try:
        return TicketClaims(
            organization_id=uuid.UUID(parts[0]),
            user_id=uuid.UUID(parts[1]),
            meeting_id=uuid.UUID(parts[2]),
        )
    except ValueError:  # pragma: no cover - only a corrupted entry
        return None


class RedisTicketStore:
    """Tickets in Redis, so any API replica can redeem one."""

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    async def issue(self, ticket: str, claims: TicketClaims) -> None:
        await self._client.set(f"{_PREFIX}{ticket}", _encode(claims), ex=TICKET_TTL_SECONDS)

    async def redeem(self, ticket: str) -> TicketClaims | None:
        # GETDEL, not GET then DELETE: two replicas redeeming the same ticket at
        # the same instant would otherwise both succeed, and "single use" would
        # be a comment rather than a fact.
        raw = await self._client.getdel(f"{_PREFIX}{ticket}")
        if raw is None:
            return None
        return _decode(raw.decode() if isinstance(raw, bytes) else str(raw))


@dataclass
class InMemoryTicketStore:
    """Tickets in a dict. One process only, which is all a test needs."""

    issued: dict[str, tuple[TicketClaims, float]] = field(default_factory=dict)

    async def issue(self, ticket: str, claims: TicketClaims) -> None:
        self.issued[ticket] = (claims, time.monotonic() + TICKET_TTL_SECONDS)

    async def redeem(self, ticket: str) -> TicketClaims | None:
        found = self.issued.pop(ticket, None)
        if found is None:
            return None
        claims, expires_at = found
        if time.monotonic() > expires_at:
            return None
        return claims
