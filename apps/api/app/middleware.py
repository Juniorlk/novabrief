"""Request-scoped context: every request carries a `debug_id`.

A client may supply its own identifier so that a meeting keeps one identity
from the desktop app through the API and the workers (ADR-07). When it does
not, one is minted here, and it is returned in the response header so the
caller can quote it even for a request it did not tag itself.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.debug_id import new_debug_id
from app.logging import bind_request_context, clear_request_context, get_logger

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
