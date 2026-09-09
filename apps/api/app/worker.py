"""The Celery application: the processing queue and its scheduler.

Run the two halves separately:

    celery -A app.worker.celery_app worker --loglevel=info
    celery -A app.worker.celery_app beat   --loglevel=info

Several settings below are load-bearing rather than decorative, and each is
there because the default would hurt:

**JSON only, never pickle.** Celery's historical default deserialises pickle
from the broker, which turns any write access to Redis into remote code
execution on every worker. There is no meeting payload that needs pickle.

**`acks_late` with `reject_on_worker_lost`.** A task is acknowledged when it
finishes, not when it is picked up, so a worker killed mid-transcription puts
the meeting back on the queue instead of losing it silently. It is safe here
because the state machine refuses a transition that already happened: a
redelivered task finds the meeting has moved on and stops.

**`prefetch_multiplier = 1`.** These tasks take minutes. The default lets one
worker reserve a batch it cannot start, so meetings sit behind a busy worker
while another sits idle.

**No result is stored by default.** A task's outcome is the meeting's state in
PostgreSQL, which the client already watches. Writing it to Redis as well would
be a second source of truth that can disagree with the first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from celery import Celery
from celery.schedules import crontab
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.logging import bind_request_context, configure_logging, get_logger

logger = get_logger(__name__)


def build_celery(settings: Settings | None = None) -> Celery:
    """Construct the Celery application from configuration."""
    resolved = settings or get_settings()
    configure_logging(resolved)

    app = Celery(
        "novabrief",
        broker=resolved.broker_url,
        backend=resolved.result_backend,
        # Listed rather than autodiscovered, and imported when the app
        # finalises rather than while it is being built. Discovering with
        # `force=True` here imports `app.tasks` in the middle of this function,
        # and every task module importing `celery_app` back is then a circular
        # import. It only fails in a real worker: a test that imports the task
        # module first has already finished loading this one.
        include=["app.tasks.maintenance", "app.tasks.pipeline"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        # Refusing every other content type is what closes the pickle hole: an
        # attacker who can write to the broker cannot make a worker unpickle.
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=resolved.celery_task_soft_time_limit_seconds,
        # A hard limit above the soft one, so a task gets the chance to fail
        # cleanly and mark its meeting FAILED before being killed outright.
        task_time_limit=resolved.celery_task_soft_time_limit_seconds + 60,
        task_ignore_result=True,
        broker_connection_retry_on_startup=True,
        beat_schedule={
            "purge-expired-organizations": {
                "task": "novabrief.purge_organizations",
                "schedule": crontab(
                    hour=resolved.purge_cron_hour, minute=resolved.purge_cron_minute
                ),
            },
        },
    )
    return app


celery_app = build_celery()


def run_async[T](work: Callable[[AsyncSession], Awaitable[T]], *, debug_id: str | None = None) -> T:
    """Run one async unit of work inside a Celery task.

    A fresh event loop, and a fresh engine created *inside* it. asyncpg
    connections belong to the loop that opened them; an engine shared across
    tasks would hand a second loop a connection it cannot await, and the
    failure surfaces far from the cause.

    Everything runs in one transaction. A task that half-succeeded would leave
    a meeting in a state its own retry cannot reconcile.
    """

    async def _run() -> T:
        settings = get_settings()
        if debug_id:
            # ADR-07: the identifier follows the meeting into the worker logs.
            bind_request_context(debug_id=debug_id)
        engine = create_engine(settings)
        try:
            factory = create_session_factory(engine)
            async with factory() as session, session.begin():
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def task_failure(name: str, exc: BaseException, **context: Any) -> None:
    """One place to log a task failure, without the payload.

    The exception type and the identifiers, never `str(exc)`: a provider error
    quotes the request it choked on, and that request carries meeting content.
    """
    logger.error("task_failed", task=name, error=type(exc).__name__, **context)
