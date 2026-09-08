"""NovaBrief API entry point.

L0 scope: the skeleton the rest of the MVP is built on — configuration,
structured logging correlated by `debug_id`, Problem Details errors, database
wiring and health probes. Authentication, organizations and meetings arrive
with L1 and L2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.errors import install_error_handlers
from app.logging import configure_logging, get_logger
from app.middleware import DEBUG_ID_HEADER, RateLimitMiddleware, RequestContextMiddleware
from app.ratelimit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter
from app.routers import auth, health

API_PREFIX = "/api/v1"
VERSION = "0.1.0"

logger = get_logger(__name__)


def _build_limiter(settings: Settings) -> RateLimiter:
    """Shared counters in Redis, or process-local ones when there is none.

    Falling back keeps a developer able to run the API without Redis. It is a
    development convenience and nothing more: with several replicas each
    process would grant the full allowance, so production must have Redis.
    """
    if not settings.redis_url:
        logger.warning("rate_limiter_in_memory", reason="no REDIS_URL configured")
        return InMemoryRateLimiter()
    # Aggressive timeouts: this call sits in front of every request, so a
    # slow Redis must fail in milliseconds rather than seconds. Without this a
    # dead Redis adds its full connect timeout to every single request.
    return RedisRateLimiter(
        Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
        )
    )


def _install_sentry(settings: Settings) -> None:
    """Wire Sentry when a DSN is configured.

    `send_default_pii` stays off: this product records meetings, and an error
    report is not a place where any of that may surface.
    """
    if not settings.sentry_dsn_api:
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn_api,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        release=VERSION,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open shared resources on startup and release them on shutdown."""
    settings = get_settings()
    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    logger.info("api_started", environment=settings.environment, version=VERSION)
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("api_stopped")


def create_app(*, limiter: RateLimiter | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance: tests build an app with
    their own settings, and nothing is constructed as a side effect of import.

    `limiter` lets a test supply its own counter. Sharing one Redis across a
    test suite makes each test depend on how many requests the previous ones
    made, which is how a suite becomes order-dependent and flaky.
    """
    settings = get_settings()
    configure_logging(settings)
    _install_sentry(settings)

    app = FastAPI(
        title="NovaBrief API",
        version=VERSION,
        summary="Meeting capture, transcription and structured reports.",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    # Middleware is applied bottom-up, so this reads in reverse: rate limiting
    # runs first, then the request context. Refusing an over-limit request
    # before it reaches a handler is the whole point — it must not get as far
    # as opening a database session.
    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter or _build_limiter(settings),
        settings=settings,
    )
    # Outermost, so the debug_id is bound before anything — including a 429 —
    # can be logged or returned.
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[DEBUG_ID_HEADER],
        )

    install_error_handlers(app)

    # Health lives outside the versioned prefix: an orchestrator probes the
    # process, not a version of the contract.
    app.include_router(health.router)
    app.include_router(auth.router, prefix=API_PREFIX)

    return app


app = create_app()
