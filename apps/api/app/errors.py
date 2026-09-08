"""API errors in the Problem Details format (RFC 9457).

Every failure leaves the API with the same shape: a stable machine-readable
`code`, the `debug_id` that ties the response to the server logs, and a human
message. The client translates the code into French or English; the API never
ships a user-facing sentence in one language only (CLAUDE.md section 6).

The stable code matters more than the prose. Messages get reworded; a client
that branches on wording breaks silently, one that branches on `QUOTA_EXCEEDED`
does not.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_logger

CONTENT_TYPE = "application/problem+json"

logger = get_logger(__name__)


class ProblemError(Exception):
    """An error that the API knows how to present to a client."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        title: str,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        self.extra = extra or {}
        super().__init__(f"{code}: {title}")


def problem_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    title: str,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build a Problem Details response carrying the request's debug_id."""
    debug_id = getattr(request.state, "debug_id", None)
    body: dict[str, Any] = {
        # A resolvable type URI is what makes the code documented rather than
        # folklore; the page is served by the web app.
        "type": f"https://docs.novabrief.com/errors/{code.lower()}",
        "title": title,
        "status": status_code,
        "code": code,
        "instance": str(request.url.path),
    }
    if detail is not None:
        body["detail"] = detail
    if debug_id is not None:
        body["debug_id"] = debug_id
    body.update(extra or {})

    return JSONResponse(status_code=status_code, content=body, media_type=CONTENT_TYPE)


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that give every failure the same shape."""

    @app.exception_handler(ProblemError)
    async def _handle_problem(request: Request, exc: ProblemError) -> JSONResponse:
        logger.warning(
            "request_failed",
            code=exc.code,
            status=exc.status_code,
            path=request.url.path,
        )
        return problem_response(
            request=request,
            status_code=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            extra=exc.extra,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(
            request=request,
            status_code=exc.status_code,
            code=_code_for_status(exc.status_code),
            title=str(exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's raw errors are not passed through. They carry the rejected
        # `input` — which on a password field is the password itself, echoed
        # back through every proxy and access log on the way out — and a `ctx`
        # holding the original exception object, which is not JSON
        # serialisable. Only the field, the type and the message leave.
        return problem_response(
            request=request,
            status_code=422,
            code="VALIDATION_ERROR",
            title="The request payload is invalid.",
            extra={"errors": _safe_validation_errors(exc)},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The client gets the debug_id and nothing else: an internal failure
        # must never leak a stack trace, a query or a file path.
        logger.exception("unhandled_exception", path=request.url.path)
        return problem_response(
            request=request,
            status_code=500,
            code="INTERNAL_ERROR",
            title="An unexpected error occurred.",
            detail="Quote the debug_id when contacting support.",
        )


def _safe_validation_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    """Reduce Pydantic's errors to what a client needs and nothing more.

    A caller needs to know which field was wrong and why. It does not need its
    own submitted value handed back, and we must not be the ones putting a
    password into a response body.
    """
    safe: list[dict[str, str]] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        safe.append(
            {
                "field": location,
                "type": str(error.get("type", "invalid")),
                "message": str(error.get("msg", "invalid value")),
            }
        )
    return safe


def _code_for_status(status_code: int) -> str:
    """Map a bare HTTP status onto a stable code."""
    # Written as integers rather than Starlette constants: the framework has
    # renamed some of them between versions, and an HTTP status number is the
    # stabler of the two spellings.
    return {
        400: "BAD_REQUEST",
        401: "UNAUTHENTICATED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMITED",
    }.get(status_code, "HTTP_ERROR")
