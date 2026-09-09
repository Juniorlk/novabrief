"""The analysis stage (EF-42, EF-44, section 18.2).

Its own task rather than a continuation of transcription: transcription is the
expensive half, and an LLM hiccup must not re-run it. A meeting that fails here
retries from ANALYZING, not from the supplier bill.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import set_current_organization
from app.logging import get_logger
from app.models import Meeting, MeetingStatus, Organization, Transcript, TranscriptSegment
from app.prompts import load
from app.providers import build_llm
from app.services import extraction, usage
from app.services.meetings import IllegalTransitionError, advance
from app.worker import celery_app, run_async, task_failure

logger = get_logger(__name__)


@celery_app.task(
    name="novabrief.analyse_meeting",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=300,
    retry_jitter=True,
)
def analyse_meeting(self: object, organization_id: str, meeting_id: str) -> str:
    """Extract the report, then publish the meeting."""
    organization_uuid = uuid.UUID(organization_id)
    meeting_uuid = uuid.UUID(meeting_id)

    async def work(session: AsyncSession) -> str:
        settings = get_settings()
        await set_current_organization(session, organization_uuid)

        meeting = await session.get(Meeting, meeting_uuid)
        organization = await session.get(Organization, organization_uuid)
        if meeting is None or organization is None:
            logger.info("analysis_skipped_missing_meeting", meeting_id=meeting_id)
            return "missing"

        if meeting.status != MeetingStatus.ANALYZING.value:
            logger.info(
                "analysis_skipped_wrong_state", meeting_id=meeting_id, status=meeting.status
            )
            return meeting.status

        transcript = await session.scalar(
            select(Transcript).where(Transcript.meeting_id == meeting_uuid)
        )
        if transcript is None:
            await _fail(session, meeting=meeting, reason="NO_TRANSCRIPT")
            return MeetingStatus.FAILED.value

        segments = list(
            (
                await session.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.transcript_id == transcript.id)
                    .order_by(TranscriptSegment.start_ms)
                )
            ).all()
        )

        prompts = load(settings.prompt_version)
        try:
            extracted = await extraction.extract_report(
                session,
                llm=build_llm(settings),
                system_prompt=prompts["system"],
                repair_prompt=prompts["repair"],
                prompt_version=settings.prompt_version,
                organization=organization,
                meeting=meeting,
                segments=segments,
            )
        except extraction.ExtractionFailedError as exc:
            # EF-45: no quota is decremented for a failed meeting.
            await _fail(session, meeting=meeting, reason=exc.code)
            logger.warning("analysis_failed", meeting_id=meeting_id, reason=exc.code)
            return MeetingStatus.FAILED.value

        await advance(session, meeting=meeting, to=MeetingStatus.COMPLETED)

        # Section 11: publication is where the quota is charged and the cost
        # recorded, in the same transaction as the report. Charging earlier
        # would bill for meetings that never produced anything.
        meeting.provider_llm = extracted.usage.model
        billed = meeting.duration_seconds
        await usage.consume(session, organization_id=organization_uuid, seconds=billed)
        await usage.record(
            session,
            organization=organization,
            meeting=meeting,
            seconds_billed=billed,
            stt_provider=meeting.provider_stt,
            stt_cost_usd=_transcription_cost(transcript),
            llm_provider=extracted.usage.model,
            llm_tokens_in=extracted.usage.tokens_in,
            llm_tokens_out=extracted.usage.tokens_out,
            llm_cost_usd=extracted.usage.cost_usd,
        )
        await advance(session, meeting=meeting, to=MeetingStatus.PUBLISHED)
        return MeetingStatus.PUBLISHED.value

    try:
        outcome = run_async(work)
    except Exception as exc:
        task_failure("novabrief.analyse_meeting", exc, meeting_id=meeting_id)
        raise

    logger.info("analyse_meeting_finished", meeting_id=meeting_id, outcome=outcome)
    return outcome


async def _fail(session: AsyncSession, *, meeting: Meeting, reason: str) -> None:
    try:
        await advance(session, meeting=meeting, to=MeetingStatus.FAILED, reason=reason)
    except IllegalTransitionError:  # pragma: no cover - defensive
        logger.error("could_not_mark_failed", meeting_id=str(meeting.id), status=meeting.status)


def _transcription_cost(transcript: Transcript) -> Decimal:
    """What the transcription of this meeting cost.

    Written by the transcription stage and read here, because the ledger entry
    covers the whole meeting and is written once, at publication. Zero when the
    router had no rate configured - an honest gap rather than a guess.
    """
    metadata = transcript.provider_metadata or {}
    try:
        return Decimal(str(metadata.get("cost_usd", "0")))
    except (ValueError, ArithmeticError):  # pragma: no cover - defensive
        return Decimal(0)
