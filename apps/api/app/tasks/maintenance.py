"""Scheduled housekeeping.

These run on the beat schedule rather than in response to a request, which
makes them the tasks nobody watches. Two consequences shape the code: every one
of them logs what it did even when it did nothing, and none of them are allowed
to fail quietly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import set_current_organization
from app.logging import get_logger
from app.services.meetings import purge_due_audio
from app.services.organizations import purge_due_organizations
from app.storage import S3StorageProvider
from app.worker import celery_app, run_async, task_failure

logger = get_logger(__name__)


@celery_app.task(
    name="novabrief.purge_organizations",
    bind=True,
    max_retries=3,
    # Exponential, with jitter: three workers retrying a database that just
    # came back would otherwise hit it in lockstep and knock it over again.
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=900,
    retry_jitter=True,
)
def purge_organizations(self: object) -> int:
    """EF-06: erase the organizations whose seven days have run out.

    Nothing here is recoverable once it runs, which is exactly why it is
    scheduled rather than triggered: a deletion should happen because a clock
    reached a date, not because somebody clicked twice.
    """

    async def work(session: AsyncSession) -> int:
        purged = await purge_due_organizations(session)
        return len(purged)

    try:
        count = run_async(work)
    except Exception as exc:
        task_failure("novabrief.purge_organizations", exc)
        raise

    # Logged even at zero: a scheduled job that only speaks when it acts is
    # indistinguishable from one that stopped running.
    logger.info("purge_organizations_ran", purged=count)
    return count


@celery_app.task(
    name="novabrief.purge_audio",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=900,
    retry_jitter=True,
)
def purge_audio(self: object) -> int:
    """ADR-06: the audio goes, the text stays.

    Sweeps tenant by tenant. A single query across every organization would
    need a session with no tenant set, and under RLS that sees nothing at all —
    so the job asks which organizations exist through the same narrow
    SECURITY DEFINER path the rest of the system uses, then scopes itself to
    each in turn. Every destructive write stays inside a policy.
    """

    async def work(session: AsyncSession) -> int:
        settings = get_settings()
        storage = S3StorageProvider(settings)

        organizations = (
            await session.execute(
                text("SELECT id FROM organizations_with_expired_audio(:now)"),
                {"now": datetime.now(UTC)},
            )
        ).all()

        total = 0
        for row in organizations:
            await set_current_organization(session, row.id)
            purged = await purge_due_audio(session, storage=storage)
            total += len(purged)
        return total

    try:
        count = run_async(work)
    except Exception as exc:
        task_failure("novabrief.purge_audio", exc)
        raise

    logger.info("purge_audio_ran", purged=count)
    return count
