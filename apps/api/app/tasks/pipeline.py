"""The processing pipeline (EF-40, EF-44, EF-45).

One task per stage rather than one task for the whole pipeline. A meeting that
fails during analysis should not be transcribed again on retry: transcription
is the expensive half, and re-running it would double the supplier bill for
every LLM hiccup.

Each task is idempotent through the state machine. A redelivered message finds
the meeting has already moved on, and `advance` refuses the transition rather
than doing the work twice.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import set_current_organization
from app.logging import get_logger
from app.models import Meeting, MeetingStatus, Organization
from app.providers import build_transcription_router
from app.services import transcription
from app.services.meetings import IllegalTransitionError, advance
from app.storage import S3StorageProvider
from app.worker import celery_app, run_async, task_failure

logger = get_logger(__name__)


@celery_app.task(
    name="novabrief.transcribe_meeting",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=300,
    retry_jitter=True,
)
def transcribe_meeting(self: object, organization_id: str, meeting_id: str) -> str:
    """Transcribe one queued meeting and hand it to the analysis stage.

    Identifiers are passed, never objects: a task argument travels through
    Redis as JSON, and a serialised ORM row would be stale by the time a worker
    picked it up.
    """
    organization_uuid = uuid.UUID(organization_id)
    meeting_uuid = uuid.UUID(meeting_id)

    async def work(session: AsyncSession) -> str:
        settings = get_settings()
        await set_current_organization(session, organization_uuid)

        meeting = await session.get(Meeting, meeting_uuid)
        organization = await session.get(Organization, organization_uuid)
        if meeting is None or organization is None:
            # Deleted between being queued and being picked up. Not an error:
            # the customer asked for it to be gone.
            logger.info("transcription_skipped_missing_meeting", meeting_id=meeting_id)
            return "missing"

        if meeting.status != MeetingStatus.QUEUED.value:
            # A redelivery, or a retry of something that already succeeded.
            logger.info(
                "transcription_skipped_wrong_state",
                meeting_id=meeting_id,
                status=meeting.status,
            )
            return meeting.status

        router = build_transcription_router(settings)
        storage = S3StorageProvider(settings)

        try:
            await transcription.transcribe_meeting(
                session,
                router=router,
                storage=storage,
                organization=organization,
                meeting=meeting,
            )
        except transcription.TranscriptionFailedError as exc:
            # A verifiable, permanent problem with this recording: the audio is
            # gone or does not match its checksum. Retrying cannot help, so the
            # meeting fails now and the customer gets a Retry button (EF-45).
            await _fail(session, meeting=meeting, reason=exc.code)
            logger.warning("transcription_failed", meeting_id=meeting_id, reason=exc.code)
            return MeetingStatus.FAILED.value

        return MeetingStatus.ANALYZING.value

    try:
        outcome = run_async(work)
    except Exception as exc:
        task_failure("novabrief.transcribe_meeting", exc, meeting_id=meeting_id)
        raise

    logger.info("transcribe_meeting_finished", meeting_id=meeting_id, outcome=outcome)
    return outcome


async def _fail(session: AsyncSession, *, meeting: Meeting, reason: str) -> None:
    """Mark a meeting failed, whatever state it reached.

    Wrapped because a failure can arrive from TRANSCRIBING or FALLBACK_STT, and
    a state machine refusing the move here would replace a clear failure with
    an obscure one.
    """
    try:
        await advance(session, meeting=meeting, to=MeetingStatus.FAILED, reason=reason)
    except IllegalTransitionError:  # pragma: no cover - defensive
        logger.error("could_not_mark_failed", meeting_id=str(meeting.id), status=meeting.status)
