"""Turning a stored recording into a transcript (EF-41, EF-45, section 18.2).

Steps 1 and 2 of section 18.2: fetch the audio, verify it, transcribe it
through the router, and write the segments down.

This is where the checksum promise from L2.2 is finally kept. The API cannot
verify a SHA-256 without reading the object, and it never touches the audio;
the worker does, so the check belongs here. The file is streamed and hashed a
chunk at a time and never held whole in memory — a two-hour recording is
hundreds of megabytes, and a worker that loads one into RAM to check it is a
worker that falls over under load.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai.transcription import TranscriptionResult, TranscriptionRouter, Utterance
from app.logging import get_logger
from app.models import Meeting, MeetingStatus, Organization, Transcript, TranscriptSegment
from app.services.meetings import advance
from app.storage import StorageProvider
from app.uuid7 import uuid7

logger = get_logger(__name__)

# Read in chunks so memory stays flat whatever the recording's size.
_HASH_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 900.0
# Section 18.2: micro-segments under a second are merged, because a supplier
# splitting "oui" into its own utterance produces a transcript nobody can read.
_MIN_SEGMENT_MS = 1000


class TranscriptionFailedError(Exception):
    """Transcription could not be completed. Carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Transcribed:
    """What the step produced."""

    transcript: Transcript
    result: TranscriptionResult
    used_fallback: bool


async def verify_audio(
    *, audio_url: str, expected_sha256: str | None, client: httpx.AsyncClient | None = None
) -> int:
    """Stream the recording, hash it, and return its size.

    Section 18.2 step 1. Refusing here costs one download; not refusing costs a
    transcription of corrupted audio, a confident report built on it, and a
    customer who believes it.

    Returns the byte count so the caller can cross-check it too.
    """
    digest = hashlib.sha256()
    total = 0

    owned = client is None
    http = client or httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_SECONDS)
    try:
        async with http.stream("GET", audio_url) as response:
            if response.status_code != 200:
                message = f"the recording could not be fetched (HTTP {response.status_code})"
                raise TranscriptionFailedError("AUDIO_UNREACHABLE", message)
            async for chunk in response.aiter_bytes(_HASH_CHUNK_BYTES):
                digest.update(chunk)
                total += len(chunk)
    except httpx.HTTPError as exc:
        message = f"the recording could not be fetched: {type(exc).__name__}"
        raise TranscriptionFailedError("AUDIO_UNREACHABLE", message) from exc
    finally:
        if owned:
            await http.aclose()

    actual = digest.hexdigest()
    if expected_sha256 and actual != expected_sha256:
        # The digest itself is not logged: it identifies the recording.
        logger.warning("audio_checksum_mismatch", size_bytes=total)
        message = "the stored recording does not match the checksum declared at upload"
        raise TranscriptionFailedError("CHECKSUM_MISMATCH", message)

    logger.info("audio_verified", size_bytes=total)
    return total


def merge_micro_segments(result: TranscriptionResult) -> TranscriptionResult:
    """Fold sub-second fragments into the neighbour that shares their speaker.

    Section 18.2 step 2. A supplier that splits an interjection into its own
    utterance produces a transcript that reads as noise; merging only within
    one speaker keeps the diarisation intact.
    """
    merged: list[Utterance] = []
    for utterance in result.utterances:
        short = utterance.end_ms - utterance.start_ms < _MIN_SEGMENT_MS
        if merged and short and merged[-1].speaker_tag == utterance.speaker_tag:
            previous = merged[-1]
            # Rebuilt rather than mutated: `model_copy` does not validate, so a
            # dict slipped in here stays a dict and only breaks much later, at
            # whatever first reads it as a model.
            merged[-1] = previous.model_copy(
                update={
                    "end_ms": utterance.end_ms,
                    "text": f"{previous.text} {utterance.text}".strip(),
                }
            )
            continue
        merged.append(utterance)

    return result.model_copy(update={"utterances": merged})


async def transcribe_meeting(
    session: AsyncSession,
    *,
    router: TranscriptionRouter,
    storage: StorageProvider,
    organization: Organization,
    meeting: Meeting,
    http_client: httpx.AsyncClient | None = None,
) -> Transcribed:
    """Run a queued meeting through transcription.

    Moves QUEUED → TRANSCRIBING, then to ANALYZING on success. When the primary
    supplier had to be replaced, the meeting passes through FALLBACK_STT first,
    so section 11's state is a fact an operator can see rather than a line in a
    log (EF-45).

    The session must already be scoped to `organization`.
    """
    if meeting.audio_key is None:
        raise TranscriptionFailedError("NO_AUDIO", "this meeting has no stored recording")

    await advance(session, meeting=meeting, to=MeetingStatus.TRANSCRIBING)

    audio_url = await storage.presign_get(key=meeting.audio_key)
    await verify_audio(
        audio_url=audio_url, expected_sha256=meeting.audio_sha256, client=http_client
    )

    result, used_fallback = await router.transcribe(
        audio_url,
        # EF-41: the organization can force a language; otherwise the supplier
        # detects it, which is what a bilingual meeting needs.
        language_hint=organization.default_language if organization.default_language else None,
        keyterms=list(organization.lexicon or []),
        # EF-31 encodes local left and remote right, so the tracks are there to
        # be told apart.
        stereo_channels=True,
    )
    result = merge_micro_segments(result)

    if used_fallback:
        await advance(
            session,
            meeting=meeting,
            to=MeetingStatus.FALLBACK_STT,
            metadata={"provider": result.provider},
        )

    transcript = await _persist(session, meeting=meeting, result=result)

    meeting.language = result.language
    meeting.provider_stt = result.provider
    if result.duration_seconds:
        meeting.duration_seconds = result.duration_seconds

    await advance(session, meeting=meeting, to=MeetingStatus.ANALYZING)

    logger.info(
        "meeting_transcribed",
        meeting_id=str(meeting.id),
        provider=result.provider,
        used_fallback=used_fallback,
        segments=len(result.utterances),
    )
    return Transcribed(transcript=transcript, result=result, used_fallback=used_fallback)


async def _persist(
    session: AsyncSession, *, meeting: Meeting, result: TranscriptionResult
) -> Transcript:
    """Write the transcript and its segments."""
    transcript = Transcript(
        id=uuid7(),
        organization_id=meeting.organization_id,
        meeting_id=meeting.id,
        language=result.language,
        raw_text=result.full_text,
        # The supplier and its model, never its raw response: that echoes the
        # transcript, and a diagnostic field is not the place for meeting text.
        provider_metadata={
            "provider": result.provider,
            "utterances": len(result.utterances),
            "duration_seconds": result.duration_seconds,
        },
    )
    session.add(transcript)
    await session.flush()

    for utterance in result.utterances:
        session.add(
            TranscriptSegment(
                id=uuid7(),
                organization_id=meeting.organization_id,
                transcript_id=transcript.id,
                speaker_tag=utterance.speaker_tag,
                start_ms=utterance.start_ms,
                end_ms=utterance.end_ms,
                text=utterance.text,
                confidence=utterance.confidence,
                channel=utterance.channel,
            )
        )

    return transcript


async def existing_transcript(session: AsyncSession, *, meeting_id: uuid.UUID) -> Transcript | None:
    """The meeting's transcript, if it already has one."""
    found: Transcript | None = await session.scalar(
        select(Transcript).where(Transcript.meeting_id == meeting_id)
    )
    return found
