"""HTTP middleware: request context and rate limiting.

A client may supply its own identifier so that a meeting keeps one identity
from the desktop app through the API and the workers (ADR-07). When it does
not, one is minted here, and it is returned in the response header so the
caller can quote it even for a request it did not tag itself.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.config import Settings
from app.debug_id import new_debug_id
from app.errors import problem_response
from app.logging import bind_request_context, clear_request_context, get_logger
from app.ratelimit import Decision, RateLimiter

DEBUG_ID_HEADER = "X-Debug-Id"

logger = get_logger(__name__)

# A client-supplied identifier is untrusted input: it lands in logs, so it is
# length-capped and character-checked rather than taken as given.
_MAX_DEBUG_ID_LENGTH = 64


def _clean_debug_id(raw: str | None) -> str | None:
    """Accept a client identifier only if it is safe to put in a log line."""
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > _MAX_DEBUG_ID_LENGTH:
        return None
    if not all(char.isalnum() or char in "-_" for char in candidate):
        return None
    return candidate


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a debug_id to the request and log its outcome."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        debug_id = _clean_debug_id(request.headers.get(DEBUG_ID_HEADER)) or new_debug_id()
        request.state.debug_id = debug_id

        clear_request_context()
        bind_request_context(debug_id=debug_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The handler in errors.py builds the response; this only makes sure
            # the failure is logged with its timing before it propagates.
            logger.exception(
                "request_error",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            clear_request_context()
            raise

        response.headers[DEBUG_ID_HEADER] = debug_id
        # The query string is left out on purpose: it can carry a search term,
        # and a search term is meeting content.
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        clear_request_context()
        return response


# Paths that count against the strict allowance. Anything that verifies a
# credential belongs here, including the refresh endpoint: a refresh token is a
# credential, and an attacker holding a stolen one should not be free to probe
# with it.
_AUTH_PREFIXES = (
    "/api/v1/auth/token",
    "/api/v1/auth/register",
    "/api/v1/auth/refresh",
    "/api/v1/auth/password",
)

# Never limited: an orchestrator probes these every few seconds, and throttling
# a health check turns a busy moment into a restart loop.
_EXEMPT_PATHS = ("/health", "/health/ready")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Refuse callers who exceed their allowance, with a 429 and Retry-After."""

    def __init__(self, app: ASGIApp, *, limiter: RateLimiter, settings: Settings) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path in _EXEMPT_PATHS:
            return await call_next(request)

        is_auth = path.startswith(_AUTH_PREFIXES)
        limit = (
            self._settings.rate_limit_auth_per_minute
            if is_auth
            else self._settings.rate_limit_api_per_minute
        )

        decision = await self._limiter.hit(self._key_for(request, is_auth=is_auth), limit)

        if not decision.allowed:
            refusal = problem_response(
                request=request,
                status_code=429,
                code="RATE_LIMITED",
                title="Too many requests.",
                detail=f"Retry in {decision.retry_after} seconds.",
            )
            refusal.headers["Retry-After"] = str(decision.retry_after)
            _apply_headers(refusal, decision)
            return refusal

        response = await call_next(request)
        _apply_headers(response, decision)
        return response

    def _key_for(self, request: Request, *, is_auth: bool) -> str:
        """Identify the caller for counting purposes.

        By account where one is known, by client address otherwise. Counting
        authenticated traffic by address would make one office share a single
        allowance, since a whole company sits behind one public IP — exactly
        the customer NovaBrief sells to.
        """
        bucket = "auth" if is_auth else "api"

        token = request.headers.get("Authorization", "")
        if token.lower().startswith("bearer ") and not is_auth:
            # Hashed rather than stored: this key reaches Redis, and a bearer
            # token in a datastore we do not treat as secret is a credential
            # sitting in the open.
            digest = hashlib.sha256(token[7:].strip().encode()).hexdigest()[:32]
            return f"rl:{bucket}:tok:{digest}"

        return f"rl:{bucket}:ip:{_client_ip(request)}"


def _client_ip(request: Request) -> str:
    """The caller's address, honouring the proxy in front of us.

    `X-Forwarded-For` is only trusted because Caddy sets it and nothing else
    reaches the API directly. Behind a different topology this would let a
    caller forge their own identity and reset their own counter.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _apply_headers(response: Response, decision: Decision) -> None:
    """Tell the caller where they stand, so a client can back off on its own."""
    response.headers["X-RateLimit-Limit"] = str(decision.limit)
    response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
