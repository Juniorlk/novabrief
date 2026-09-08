"""Liveness and readiness.

Two endpoints rather than one, because they answer different questions and a
deployment that conflates them restarts healthy containers:

* ``/health`` — is the process up? No dependency is touched, so an orchestrator
  never kills the API because the database is briefly unreachable.
* ``/health/ready`` — can it actually serve? The database is queried, so a
  load balancer stops sending traffic to an instance that cannot answer.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


class Health(BaseModel):
    """Liveness answer."""

    status: Literal["ok"]
    environment: str
    version: str


class Readiness(BaseModel):
    """Readiness answer, with the state of each dependency."""

    status: Literal["ready", "degraded"]
    checks: dict[str, str]


@router.get("/health", response_model=Health, summary="Liveness probe")
async def health(
    settings: Annotated[Settings, Depends(get_settings)],
    request: Request,
) -> Health:
    """Report that the process is running."""
    return Health(
        status="ok",
        environment=settings.environment,
        version=request.app.version,
    )


@router.get("/health/ready", response_model=Readiness, summary="Readiness probe")
async def readiness(request: Request, response: Response) -> Readiness:
    """Report whether the API can serve requests."""
    checks: dict[str, str] = {}

    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:
        checks["database"] = "not configured"
    else:
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            # The exception type is enough to diagnose; its message can carry
            # the connection string, which holds a password.
            checks["database"] = f"unavailable ({type(exc).__name__})"
            logger.warning("readiness_check_failed", dependency="database")

    ready = all(value == "ok" for value in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(status="ready" if ready else "degraded", checks=checks)
