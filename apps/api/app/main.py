"""NovaBrief API entry point.

L0 scope: the skeleton the rest of the MVP is built on — configuration,
structured logging correlated by `debug_id`, Problem Details errors, database
wiring and health probes. Authentication, organizations and meetings arrive
with L1 and L2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.email import ConsoleProvider, EmailProvider, ResendProvider
from app.errors import install_error_handlers
from app.logging import configure_logging, get_logger
from app.middleware import DEBUG_ID_HEADER, RateLimitMiddleware, RequestContextMiddleware
from app.ratelimit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter
from app.routers import auth, health, meetings, members, organizations, status
from app.services.meetings import Dispatch
from app.storage import InMemoryStorageProvider, S3StorageProvider, StorageProvider
from app.tickets import InMemoryTicketStore, RedisTicketStore, TicketStore

API_PREFIX = "/api/v1"
VERSION = "0.1.0"

logger = get_logger(__name__)


def _build_email_provider(settings: Settings) -> EmailProvider:
    """Resend when a key is configured, otherwise print to the console.

    Printing rather than silently dropping: a developer without a key still
    sees the invitation link and can follow it. Silently succeeding would make
    a broken configuration look like a working one.
    """
    if not settings.resend_api_key:
        logger.warning("email_console_provider", reason="no RESEND_API_KEY configured")
        return ConsoleProvider()
    return ResendProvider(
        api_key=settings.resend_api_key,
        sender=settings.email_from,
        reply_to=settings.email_reply_to,
    )


def _build_storage(settings: Settings) -> StorageProvider:
    """The object store, or an in-memory stand-in for a developer without one.

    Unlike email, a missing bucket is not something to paper over silently in
    production: an upload that appears to succeed and stores nothing would lose
    a customer's meeting. The stand-in is therefore refused outside dev.
    """
    try:
        return S3StorageProvider(settings)
    except RuntimeError:
        if settings.environment != "dev":
            raise
        logger.warning("storage_in_memory_provider", reason="no R2 credentials configured")
        return InMemoryStorageProvider()


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
        # The rate limiter holds a Redis pool; leaving it open means sockets
        # Redis still believes are live after every restart.
        limiter = getattr(app.state, "limiter", None)
        if isinstance(limiter, RedisRateLimiter):
            await limiter.aclose()
        logger.info("api_stopped")


# Vendored into the image by infra/api.Dockerfile, absent on a laptop.
_STATIC_DIR = Path("/srv/static")
_SWAGGER_CDN = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14"


def _install_docs(app: FastAPI) -> None:
    """Serve the interactive documentation from our own origin when we can.

    FastAPI's default page pulls Swagger from a public CDN. That page sits on
    the same host as the authentication API and is where a person pastes an
    access token, so a third-party script there runs in the worst possible
    place — and it is why the Content-Security-Policy of `infra/Caddyfile` was
    blocking it, correctly, leaving a blank page.

    In the image the files are vendored and checksummed, so the policy can stay
    at 'self'. On a laptop they are absent and the CDN is used instead: there
    is no production traffic and no reverse proxy imposing a policy, so the
    trade does not apply.
    """
    vendored = _STATIC_DIR.is_dir()
    if vendored:
        app.mount(f"{API_PREFIX}/static", StaticFiles(directory=_STATIC_DIR), name="static")
        js_url = f"{API_PREFIX}/static/swagger-ui-bundle.js"
        css_url = f"{API_PREFIX}/static/swagger-ui.css"
    else:
        logger.info("swagger_assets_from_cdn", reason="no vendored copy in /srv/static")
        js_url = f"{_SWAGGER_CDN}/swagger-ui-bundle.js"
        css_url = f"{_SWAGGER_CDN}/swagger-ui.css"

    @app.get(f"{API_PREFIX}/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=f"{API_PREFIX}/openapi.json",
            title="NovaBrief API",
            swagger_js_url=js_url,
            swagger_css_url=css_url,
            # A transparent pixel. FastAPI's default points at tiangolo.com,
            # which is a third-party request for an icon; a path to a file we
            # do not ship would just be a 404 in everyone's console.
            swagger_favicon_url=(
                "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
            ),
        )


def create_app(
    *,
    limiter: RateLimiter | None = None,
    email_provider: EmailProvider | None = None,
    storage: StorageProvider | None = None,
    dispatch: Dispatch | None = None,
    tickets: TicketStore | None = None,
) -> FastAPI:
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
        # Replaced below by a page that serves Swagger from this origin.
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    _install_docs(app)

    # Middleware is applied bottom-up, so this reads in reverse: rate limiting
    # runs first, then the request context. Refusing an over-limit request
    # before it reaches a handler is the whole point — it must not get as far
    # as opening a database session.
    active_limiter = limiter or _build_limiter(settings)
    # Kept on the app so shutdown can release its connection pool.
    app.state.limiter = active_limiter
    app.add_middleware(RateLimitMiddleware, limiter=active_limiter, settings=settings)
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

    # Attached to the app rather than resolved per request: building a
    # provider is configuration, not request state.
    app.state.email_provider = email_provider or _build_email_provider(settings)
    app.state.storage = storage or _build_storage(settings)
    # None means the real queue. A test passes a recorder instead, so no
    # unit test needs a broker to run.
    app.state.dispatch = dispatch
    # In memory outside production: a status socket is not worth making a
    # developer run Redis, and a test certainly should not need one.
    app.state.tickets = tickets or (
        RedisTicketStore(Redis.from_url(settings.redis_url))
        if settings.environment != "dev"
        else InMemoryTicketStore()
    )

    install_error_handlers(app)

    # Health lives outside the versioned prefix: an orchestrator probes the
    # process, not a version of the contract.
    app.include_router(health.router)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(members.router, prefix=API_PREFIX)
    app.include_router(organizations.router, prefix=API_PREFIX)
    app.include_router(meetings.router, prefix=API_PREFIX)
    app.include_router(status.router, prefix=API_PREFIX)

    return app


app = create_app()
