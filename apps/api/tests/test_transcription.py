"""Transcription, the fallback, and the circuit breaker (lot L2.5).

No supplier is called. Novafrik cancelled the validation campaign on
2026-09-08, so what is proved here is the machinery around the suppliers — the
order they are tried in, when one is skipped, what happens when both fail, and
that a corrupted recording is never transcribed. What is *not* proved, and
cannot be until real calls are made, is the quality of the transcription
itself: EF-41's word error rate stays unmeasured.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ai.transcription import TranscriptionError, TranscriptionRouter
from ai.transcription.fake import FakeTranscriptionProvider, sample_result
from ai.transcription.router import (
    FAILURE_THRESHOLD,
    HALF_OPEN_AFTER_SECONDS,
    AllProvidersFailedError,
    Breaker,
)
from app.config import Settings, get_settings
from app.db import create_session_factory, set_current_organization
from app.models import Meeting, MeetingStatus, Organization, Transcript, TranscriptSegment, User
from app.services import transcription
from app.services.meetings import declare
from app.storage import InMemoryStorageProvider
from app.uuid7 import uuid7

pytestmark = pytest.mark.asyncio

APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)


# --------------------------------------------------------------------------
# The router: order, fallback, and giving up
# --------------------------------------------------------------------------


async def test_the_first_provider_is_preferred() -> None:
    primary = FakeTranscriptionProvider(provider_name="primary")
    secondary = FakeTranscriptionProvider(provider_name="secondary")
    router = TranscriptionRouter([primary, secondary])

    result, used_fallback = await router.transcribe("https://audio")

    assert result.provider == "primary"
    assert used_fallback is False
    assert not secondary.calls


async def test_a_failing_provider_falls_through_to_the_next() -> None:
    """EF-45: an outage at the primary is a slower meeting, not a failed one."""
    primary = FakeTranscriptionProvider(
        provider_name="primary", failures=[TranscriptionError("503")]
    )
    secondary = FakeTranscriptionProvider(provider_name="secondary")
    router = TranscriptionRouter([primary, secondary])

    result, used_fallback = await router.transcribe("https://audio")

    assert result.provider == "secondary"
    assert used_fallback is True, "the meeting must be marked as rescued"


async def test_a_rejected_request_is_not_retried_elsewhere() -> None:
    """A 4xx means the request is wrong; the next supplier rejects it too.

    Failing over would spend money to fail twice, and hide the real cause.
    """
    primary = FakeTranscriptionProvider(
        provider_name="primary",
        failures=[TranscriptionError("bad request", retryable=False)],
    )
    secondary = FakeTranscriptionProvider(provider_name="secondary")
    router = TranscriptionRouter([primary, secondary])

    with pytest.raises(TranscriptionError):
        await router.transcribe("https://audio")

    assert not secondary.calls


async def test_when_every_provider_fails_the_reasons_are_kept() -> None:
    """An operator needs to know it was both, and why each one refused."""
    primary = FakeTranscriptionProvider(
        provider_name="primary", failures=[TranscriptionError("timeout")]
    )
    secondary = FakeTranscriptionProvider(
        provider_name="secondary", failures=[TranscriptionError("503")]
    )
    router = TranscriptionRouter([primary, secondary])

    with pytest.raises(AllProvidersFailedError) as raised:
        await router.transcribe("https://audio")

    assert set(raised.value.attempts) == {"primary", "secondary"}


async def test_the_lexicon_reaches_the_provider() -> None:
    """EF-05's whole point: local proper nouns spelled correctly."""
    primary = FakeTranscriptionProvider(provider_name="primary")
    router = TranscriptionRouter([primary])

    await router.transcribe("https://audio", keyterms=["Novafrik", "Douala"], language_hint="fr")

    assert primary.calls[0]["keyterms"] == ["Novafrik", "Douala"]
    assert primary.calls[0]["language_hint"] == "fr"


async def test_the_cost_is_priced_from_the_duration() -> None:
    """Neither supplier returns a cost, and ADR-08 needs one in the ledger."""
    provider = FakeTranscriptionProvider(
        provider_name="primary",
        result=sample_result(duration_seconds=3600).model_copy(update={"cost_usd": Decimal(0)}),
    )
    router = TranscriptionRouter([provider], price_per_hour_usd=Decimal("0.36"))

    result, _ = await router.transcribe("https://audio")

    assert result.cost_usd == Decimal("0.36000")


async def test_an_unpriced_router_leaves_the_cost_at_zero() -> None:
    """A guessed rate would silently corrupt the margin dashboard."""
    provider = FakeTranscriptionProvider(
        provider_name="primary",
        result=sample_result().model_copy(update={"cost_usd": Decimal(0)}),
    )
    router = TranscriptionRouter([provider])

    result, _ = await router.transcribe("https://audio")

    assert result.cost_usd == Decimal(0)


async def test_a_router_without_providers_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        TranscriptionRouter([])


# --------------------------------------------------------------------------
# The circuit breaker
# --------------------------------------------------------------------------


async def test_the_breaker_opens_after_the_threshold() -> None:
    """Section 18.1: five failures inside ten minutes."""
    breaker = Breaker()

    for index in range(FAILURE_THRESHOLD - 1):
        breaker.record_failure(now=float(index))
    assert not breaker.is_open(now=10.0)

    breaker.record_failure(now=float(FAILURE_THRESHOLD))
    assert breaker.is_open(now=float(FAILURE_THRESHOLD))


async def test_old_failures_stop_counting() -> None:
    """Five failures spread over a day are not an outage."""
    breaker = Breaker()

    for index in range(FAILURE_THRESHOLD):
        breaker.record_failure(now=index * 1000.0)

    assert not breaker.is_open(now=FAILURE_THRESHOLD * 1000.0)


async def test_the_breaker_reopens_the_door_after_two_minutes() -> None:
    """Half-open matters as much as open: a breaker that never retries is off."""
    breaker = Breaker()
    for index in range(FAILURE_THRESHOLD):
        breaker.record_failure(now=float(index))

    assert breaker.is_open(now=10.0)
    assert not breaker.is_open(now=10.0 + HALF_OPEN_AFTER_SECONDS)


async def test_a_success_closes_the_breaker() -> None:
    breaker = Breaker()
    for index in range(FAILURE_THRESHOLD):
        breaker.record_failure(now=float(index))

    breaker.record_success(now=100.0)

    assert not breaker.is_open(now=100.0)


async def test_an_open_breaker_skips_the_provider_entirely() -> None:
    """A supplier having a bad hour is skipped, not asked on every meeting."""
    primary = FakeTranscriptionProvider(
        provider_name="primary",
        failures=[TranscriptionError("503") for _ in range(FAILURE_THRESHOLD)],
    )
    secondary = FakeTranscriptionProvider(provider_name="secondary")
    router = TranscriptionRouter([primary, secondary])

    for _ in range(FAILURE_THRESHOLD):
        await router.transcribe("https://audio")

    calls_before = len(primary.calls)
    await router.transcribe("https://audio")

    assert not router.is_available("primary")
    assert len(primary.calls) == calls_before, "an open breaker still called the provider"


# --------------------------------------------------------------------------
# Verifying the recording
# --------------------------------------------------------------------------


async def test_a_matching_checksum_passes() -> None:
    """The promise left open in L2.2, kept where the bytes actually exist."""
    body = b"pretend this is opus" * 100
    digest = hashlib.sha256(body).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        size = await transcription.verify_audio(
            audio_url="https://audio", expected_sha256=digest, client=client
        )
    finally:
        await client.aclose()

    assert size == len(body)


async def test_a_corrupted_recording_is_refused() -> None:
    """Transcribing it would produce a confident report built on damaged audio."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not what was uploaded")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(transcription.TranscriptionFailedError) as raised:
            await transcription.verify_audio(
                audio_url="https://audio", expected_sha256="0" * 64, client=client
            )
    finally:
        await client.aclose()

    assert raised.value.code == "CHECKSUM_MISMATCH"


async def test_an_unreachable_recording_is_reported_as_such() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(transcription.TranscriptionFailedError) as raised:
            await transcription.verify_audio(
                audio_url="https://audio", expected_sha256=None, client=client
            )
    finally:
        await client.aclose()

    assert raised.value.code == "AUDIO_UNREACHABLE"


# --------------------------------------------------------------------------
# Cleaning up what the supplier returns
# --------------------------------------------------------------------------


async def test_sub_second_fragments_are_merged_into_their_speaker() -> None:
    """Section 18.2: a supplier splitting "oui" into its own turn reads as noise."""
    result = sample_result()
    fragmented = result.model_copy(
        update={
            "utterances": [
                result.utterances[0].model_copy(update={"start_ms": 0, "end_ms": 3000}),
                result.utterances[0].model_copy(
                    update={"start_ms": 3000, "end_ms": 3400, "text": "oui"}
                ),
            ]
        }
    )

    merged = transcription.merge_micro_segments(fragmented)

    assert len(merged.utterances) == 1
    assert merged.utterances[0].text.endswith("oui")
    assert merged.utterances[0].end_ms == 3400


async def test_a_fragment_from_another_speaker_is_kept() -> None:
    """Merging across speakers would destroy the diarisation."""
    result = sample_result()
    fragmented = result.model_copy(
        update={
            "utterances": [
                result.utterances[0].model_copy(update={"start_ms": 0, "end_ms": 3000}),
                result.utterances[1].model_copy(
                    update={"start_ms": 3000, "end_ms": 3400, "text": "oui"}
                ),
            ]
        }
    )

    merged = transcription.merge_micro_segments(fragmented)

    assert len(merged.utterances) == 2


# --------------------------------------------------------------------------
# The whole step, against the database
# --------------------------------------------------------------------------


@dataclass
class Fixture:
    engine: AsyncEngine
    organization_id: uuid.UUID
    meeting_id: uuid.UUID
    storage: InMemoryStorageProvider
    audio_body: bytes


@pytest_asyncio.fixture
async def prepared(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fixture]:
    """An organization with one queued meeting whose audio is in the store."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv(
        "JWT_PRIVATE_KEY",
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
    )
    monkeypatch.setenv("DATABASE_URL", APP_DATABASE_URL)
    get_settings.cache_clear()

    engine = create_async_engine(APP_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"no test database reachable ({type(exc).__name__})")

    body = b"pretend this is opus audio" * 500
    organization_id = uuid7()
    factory = create_session_factory(engine)

    async with factory() as session, session.begin():
        await set_current_organization(session, organization_id)
        organization = Organization(id=organization_id, name="Test Org", default_language="fr")
        session.add(organization)
        await session.flush()
        author = User(
            id=uuid7(),
            organization_id=organization_id,
            email=f"owner-{uuid.uuid4().hex[:8]}@test.cm",
            password_hash="x",
            full_name="Owner",
            role="OWNER",
        )
        session.add(author)
        await session.flush()

        meeting = await declare(
            session,
            organization=organization,
            author=author,
            started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        meeting.audio_key = f"org/{organization_id}/meetings/{meeting.id}/audio.ogg"
        meeting.audio_sha256 = hashlib.sha256(body).hexdigest()
        meeting.status = MeetingStatus.QUEUED.value
        meeting_id = meeting.id

    storage = InMemoryStorageProvider()
    yield Fixture(
        engine=engine,
        organization_id=organization_id,
        meeting_id=meeting_id,
        storage=storage,
        audio_body=body,
    )

    await engine.dispose()
    get_settings.cache_clear()


def _audio_client(body: bytes) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_transcribing_writes_the_segments_and_advances(prepared: Fixture) -> None:
    router = TranscriptionRouter([FakeTranscriptionProvider(provider_name="primary")])
    client = _audio_client(prepared.audio_body)
    factory = create_session_factory(prepared.engine)

    try:
        async with factory() as session, session.begin():
            await set_current_organization(session, prepared.organization_id)
            organization = await session.get(Organization, prepared.organization_id)
            meeting = await session.get(Meeting, prepared.meeting_id)
            assert organization is not None and meeting is not None

            await transcription.transcribe_meeting(
                session,
                router=router,
                storage=prepared.storage,
                organization=organization,
                meeting=meeting,
                http_client=client,
            )
    finally:
        await client.aclose()

    async with factory() as session:
        await set_current_organization(session, prepared.organization_id)
        meeting = await session.get(Meeting, prepared.meeting_id)
        transcript = await session.scalar(
            select(Transcript).where(Transcript.meeting_id == prepared.meeting_id)
        )
        segments = (
            await session.scalars(
                select(TranscriptSegment).where(
                    TranscriptSegment.transcript_id == (transcript.id if transcript else None)
                )
            )
        ).all()

    assert meeting is not None
    assert meeting.status == MeetingStatus.ANALYZING.value
    assert meeting.provider_stt == "primary"
    assert transcript is not None
    assert len(segments) == 2


async def test_a_fallback_leaves_a_visible_trace(prepared: Fixture) -> None:
    """EF-45 asks for the incident to be traced; section 11 gives it a state."""
    router = TranscriptionRouter(
        [
            FakeTranscriptionProvider(
                provider_name="primary", failures=[TranscriptionError("503")]
            ),
            FakeTranscriptionProvider(provider_name="secondary"),
        ]
    )
    client = _audio_client(prepared.audio_body)
    factory = create_session_factory(prepared.engine)

    try:
        async with factory() as session, session.begin():
            await set_current_organization(session, prepared.organization_id)
            organization = await session.get(Organization, prepared.organization_id)
            meeting = await session.get(Meeting, prepared.meeting_id)
            assert organization is not None and meeting is not None
            outcome = await transcription.transcribe_meeting(
                session,
                router=router,
                storage=prepared.storage,
                organization=organization,
                meeting=meeting,
                http_client=client,
            )
    finally:
        await client.aclose()

    assert outcome.used_fallback is True

    async with prepared.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": str(prepared.organization_id)},
        )
        rows = await connection.execute(
            text(
                "SELECT metadata::text AS payload FROM audit_log "
                "WHERE action = 'meeting.transition' ORDER BY id"
            )
        )
        trail = " ".join(row.payload for row in rows)

    assert "FALLBACK_STT" in trail, "a rescued meeting left no trace"


async def test_a_corrupted_recording_never_reaches_a_provider(prepared: Fixture) -> None:
    """The check is worth nothing if the supplier is called anyway."""
    provider = FakeTranscriptionProvider(provider_name="primary")
    router = TranscriptionRouter([provider])
    client = _audio_client(b"corrupted, not what was uploaded")
    factory = create_session_factory(prepared.engine)

    try:
        async with factory() as session, session.begin():
            await set_current_organization(session, prepared.organization_id)
            organization = await session.get(Organization, prepared.organization_id)
            meeting = await session.get(Meeting, prepared.meeting_id)
            assert organization is not None and meeting is not None

            with pytest.raises(transcription.TranscriptionFailedError):
                await transcription.transcribe_meeting(
                    session,
                    router=router,
                    storage=prepared.storage,
                    organization=organization,
                    meeting=meeting,
                    http_client=client,
                )
    finally:
        await client.aclose()

    assert not provider.calls, "a corrupted recording was sent to a supplier"


async def test_the_organizations_lexicon_is_passed_through(prepared: Fixture) -> None:
    provider = FakeTranscriptionProvider(provider_name="primary")
    router = TranscriptionRouter([provider])
    client = _audio_client(prepared.audio_body)
    factory = create_session_factory(prepared.engine)

    try:
        async with factory() as session, session.begin():
            await set_current_organization(session, prepared.organization_id)
            organization = await session.get(Organization, prepared.organization_id)
            meeting = await session.get(Meeting, prepared.meeting_id)
            assert organization is not None and meeting is not None
            organization.lexicon = ["Novafrik", "RCCM"]
            await transcription.transcribe_meeting(
                session,
                router=router,
                storage=prepared.storage,
                organization=organization,
                meeting=meeting,
                http_client=client,
            )
    finally:
        await client.aclose()

    assert provider.calls[0]["keyterms"] == ["Novafrik", "RCCM"]


# --------------------------------------------------------------------------
# Building the chain from configuration
# --------------------------------------------------------------------------


async def test_a_provider_without_a_key_is_left_out() -> None:
    """An empty slot would burn a retry on every meeting for no benefit."""
    from app.providers import build_transcription_router

    settings = Settings(assemblyai_api_key="key", deepgram_api_key=None)

    router = build_transcription_router(settings)

    assert router.provider_names == ["assemblyai:universal-3.5"]


async def test_the_order_is_configuration() -> None:
    """Section 18.1: reorderable without a deployment."""
    from app.providers import build_transcription_router

    settings = Settings(
        assemblyai_api_key="key",
        deepgram_api_key="key",
        transcription_provider_order=["deepgram", "assemblyai"],
    )

    router = build_transcription_router(settings)

    assert router.provider_names[0].startswith("deepgram")


async def test_a_worker_with_no_provider_refuses_to_start() -> None:
    """Accepting meetings it cannot process would look like a supplier outage."""
    from app.providers import build_transcription_router

    settings = Settings(assemblyai_api_key=None, deepgram_api_key=None)

    with pytest.raises(RuntimeError, match="no transcription provider"):
        build_transcription_router(settings)
