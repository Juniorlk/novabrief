"""Realtime status, retry and audio retention (lots L2.7 and L2.8).

The ticket is the interesting part. It exists because a browser cannot set an
Authorization header on a WebSocket, and the usual workarounds — a token in the
query string, a cookie — are worse than the problem. So most of what is checked
here is what a ticket refuses to do: work twice, outlive its minute, or open a
socket onto a meeting it was not issued for.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import create_session_factory, set_current_organization
from app.email import RecordingProvider
from app.main import create_app
from app.models import Meeting, MeetingStatus, Organization
from app.ratelimit import InMemoryRateLimiter
from app.services import meetings as meetings_service
from app.storage import InMemoryStorageProvider
from app.tickets import InMemoryTicketStore, TicketClaims, new_ticket

pytestmark = pytest.mark.asyncio

APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)

PREFIX = "/api/v1"
PASSWORD = "un mot de passe long"


@dataclass
class Harness:
    client: AsyncClient
    engine: AsyncEngine
    storage: InMemoryStorageProvider
    tickets: InMemoryTicketStore
    dispatched: list[tuple[uuid.UUID, uuid.UUID]]


@pytest_asyncio.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv(
        "JWT_PRIVATE_KEY",
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
    )
    monkeypatch.setenv(
        "JWT_PUBLIC_KEY",
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
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

    storage = InMemoryStorageProvider()
    tickets = InMemoryTicketStore()
    dispatched: list[tuple[uuid.UUID, uuid.UUID]] = []

    def record(*, organization_id: uuid.UUID, meeting_id: uuid.UUID) -> None:
        dispatched.append((organization_id, meeting_id))

    app = create_app(
        limiter=InMemoryRateLimiter(),
        email_provider=RecordingProvider(),
        storage=storage,
        dispatch=record,
        tickets=tickets,
    )
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        yield Harness(
            client=http,
            engine=engine,
            storage=storage,
            tickets=tickets,
            dispatched=dispatched,
        )

    await engine.dispose()
    get_settings.cache_clear()


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@test.cm"


async def _owner(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Owner",
            "organization_name": "Test Org",
            "password": PASSWORD,
            "email": _email(),
        },
    )
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    body = (await client.get(f"{PREFIX}/me", headers=headers)).json()
    return body["organization"]["id"], headers


async def _declare(client: AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        f"{PREFIX}/meetings",
        json={"started_at": datetime.now(UTC).isoformat()},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    identifier: str = response.json()["id"]
    return identifier


# The only columns a test may set directly. A closed list rather than whatever
# the caller passed: this builds a statement by hand, and the names are the one
# part that cannot be bound as a parameter.
_FORCEABLE = frozenset(
    {"audio_key", "audio_upload_id", "purge_at", "failed_reason", "duration_seconds"}
)


async def _force_status(
    api: Harness, organization_id: str, meeting_id: str, status: str, **columns: object
) -> None:
    """Put a meeting in a state directly, to test what follows it."""
    unknown = set(columns) - _FORCEABLE
    assert not unknown, f"refusing to set {unknown}"

    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        # Core rather than a formatted string: nothing here is concatenated, so
        # there is no statement to get wrong and no linter to argue with.
        await connection.execute(
            update(Meeting)
            .where(Meeting.id == uuid.UUID(meeting_id))
            .values(status=status, **columns)
        )
        await connection.commit()


def _app_of(harness: Harness) -> object:
    """The ASGI application behind the test client."""
    return harness.client._transport.app  # type: ignore[attr-defined]


async def _open_socket(harness: Harness, path: str) -> dict[str, object] | str:
    """Open the status socket and read one frame, or report the refusal.

    Driven from a worker thread: `TestClient` runs its own event loop, and
    calling it from inside the one pytest-asyncio already started deadlocks on
    the portal. Returning a string instead of raising keeps the assertion in
    the test rather than in this helper.
    """
    import asyncio

    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    application = _app_of(harness)

    def connect() -> dict[str, object] | str:
        with TestClient(application) as client:  # type: ignore[arg-type]
            try:
                with client.websocket_connect(path) as socket:
                    frame: dict[str, object] = socket.receive_json()
                    return frame
            except WebSocketDisconnect as exc:
                return f"refused:{exc.code}"

    return await asyncio.get_running_loop().run_in_executor(None, connect)


# --------------------------------------------------------------------------
# The ticket
# --------------------------------------------------------------------------


async def test_a_ticket_is_issued_for_a_meeting(api: Harness) -> None:
    _, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)

    response = await api.client.post(f"{PREFIX}/meetings/{meeting_id}/ws-ticket", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["ticket"].startswith("wst_")
    assert response.json()["expires_in_seconds"] == 60


async def test_a_ticket_needs_a_session(api: Harness) -> None:
    """Otherwise the ticket would be the authentication, not a consequence of it."""
    _, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)

    response = await api.client.post(f"{PREFIX}/meetings/{meeting_id}/ws-ticket")

    assert response.status_code == 401


async def test_no_ticket_for_another_tenants_meeting(api: Harness) -> None:
    _, first = await _owner(api.client)
    _, second = await _owner(api.client)
    meeting_id = await _declare(api.client, first)

    response = await api.client.post(f"{PREFIX}/meetings/{meeting_id}/ws-ticket", headers=second)

    assert response.status_code == 404


async def test_a_ticket_works_exactly_once() -> None:
    """The property the whole design rests on."""
    store = InMemoryTicketStore()
    ticket = new_ticket()
    claims = TicketClaims(
        organization_id=uuid.uuid4(), user_id=uuid.uuid4(), meeting_id=uuid.uuid4()
    )
    await store.issue(ticket, claims)

    assert await store.redeem(ticket) == claims
    assert await store.redeem(ticket) is None


async def test_an_unknown_ticket_redeems_to_nothing() -> None:
    store = InMemoryTicketStore()

    assert await store.redeem("wst_never_issued") is None


# --------------------------------------------------------------------------
# The socket
# --------------------------------------------------------------------------


async def test_the_socket_refuses_a_missing_ticket(api: Harness) -> None:
    """Refused before accepting, so an unauthenticated client never gets a socket."""
    _, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)

    outcome = await _open_socket(api, f"{PREFIX}/ws/meetings/{meeting_id}")

    # 1008 is "policy violation".
    assert outcome == "refused:1008"


async def test_the_socket_refuses_a_ticket_for_another_meeting(api: Harness) -> None:
    """A ticket grants one thing: watching the meeting it was issued for."""
    _, headers = await _owner(api.client)
    watched = await _declare(api.client, headers)
    other = await _declare(api.client, headers)

    ticket = (
        await api.client.post(f"{PREFIX}/meetings/{watched}/ws-ticket", headers=headers)
    ).json()["ticket"]

    outcome = await _open_socket(api, f"{PREFIX}/ws/meetings/{other}?ticket={ticket}")

    assert outcome == "refused:1008"


async def test_the_socket_pushes_the_state_and_closes_when_final(api: Harness) -> None:
    """EF-44, and the reason the socket ends itself.

    A client holding a connection open on a finished meeting is a connection
    nobody will ever close.
    """
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    await _force_status(api, organization_id, meeting_id, MeetingStatus.PUBLISHED.value)

    ticket = (
        await api.client.post(f"{PREFIX}/meetings/{meeting_id}/ws-ticket", headers=headers)
    ).json()["ticket"]

    frame = await _open_socket(api, f"{PREFIX}/ws/meetings/{meeting_id}?ticket={ticket}")

    assert isinstance(frame, dict)
    assert frame["status"] == "PUBLISHED"
    assert frame["meeting_id"] == meeting_id


async def test_a_failed_meeting_says_why_on_the_socket(api: Harness) -> None:
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    await _force_status(
        api,
        organization_id,
        meeting_id,
        MeetingStatus.FAILED.value,
        failed_reason="CHECKSUM_MISMATCH",
    )
    ticket = (
        await api.client.post(f"{PREFIX}/meetings/{meeting_id}/ws-ticket", headers=headers)
    ).json()["ticket"]

    frame = await _open_socket(api, f"{PREFIX}/ws/meetings/{meeting_id}?ticket={ticket}")

    assert isinstance(frame, dict)
    assert frame["status"] == "FAILED"
    assert frame["failed_reason"] == "CHECKSUM_MISMATCH"


# --------------------------------------------------------------------------
# Retry (EF-45)
# --------------------------------------------------------------------------


async def test_a_failed_meeting_can_be_retried(api: Harness) -> None:
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    await _force_status(
        api, organization_id, meeting_id, MeetingStatus.FAILED.value, failed_reason="LLM_REFUSED"
    )

    response = await api.client.post(f"{PREFIX}/meetings/{meeting_id}/retry", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "QUEUED"
    # Cleared, so the next failure is not confused with the last one.
    assert response.json()["failed_reason"] is None
    assert [str(pair[1]) for pair in api.dispatched] == [meeting_id]


async def test_a_healthy_meeting_cannot_be_retried(api: Harness) -> None:
    """Re-running it would pay for a second transcription and overwrite a report."""
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    await _force_status(api, organization_id, meeting_id, MeetingStatus.PUBLISHED.value)

    response = await api.client.post(f"{PREFIX}/meetings/{meeting_id}/retry", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "NOT_FAILED"
    assert api.dispatched == []


# --------------------------------------------------------------------------
# Audio retention (ADR-06)
# --------------------------------------------------------------------------


async def test_the_retention_comes_from_the_organization(api: Harness) -> None:
    """Never a constant: what a customer bought is data (ADR-09)."""
    organization = Organization(id=uuid.uuid4(), name="x", audio_retention_days=90)
    now = datetime(2026, 9, 9, tzinfo=UTC)

    assert meetings_service.retention_deadline(organization, now=now) == now + timedelta(days=90)


async def test_expired_audio_is_purged_and_the_text_survives(api: Harness) -> None:
    """ADR-06 in one assertion: the recording goes, the meeting stays."""
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    key = f"org/{organization_id}/meetings/{meeting_id}/audio.ogg"
    api.storage.objects[key] = 1024
    await _force_status(
        api,
        organization_id,
        meeting_id,
        MeetingStatus.PUBLISHED.value,
        audio_key=key,
        purge_at=datetime.now(UTC) - timedelta(days=1),
    )

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        purged = await meetings_service.purge_due_audio(session, storage=api.storage)

    assert [str(identifier) for identifier in purged] == [meeting_id]
    assert key not in api.storage.objects

    detail = await api.client.get(f"{PREFIX}/meetings/{meeting_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "AUDIO_PURGED"


async def test_audio_inside_its_retention_is_left_alone(api: Harness) -> None:
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    key = f"org/{organization_id}/meetings/{meeting_id}/audio.ogg"
    api.storage.objects[key] = 1024
    await _force_status(
        api,
        organization_id,
        meeting_id,
        MeetingStatus.PUBLISHED.value,
        audio_key=key,
        purge_at=datetime.now(UTC) + timedelta(days=10),
    )

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        purged = await meetings_service.purge_due_audio(session, storage=api.storage)

    assert purged == []
    assert key in api.storage.objects


async def test_a_failed_deletion_does_not_mark_the_audio_purged(api: Harness) -> None:
    """Claiming the recording is gone while it sits in the bucket is the one
    outcome ADR-06 cannot tolerate."""
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    key = f"org/{organization_id}/meetings/{meeting_id}/audio.ogg"
    api.storage.objects[key] = 1024
    await _force_status(
        api,
        organization_id,
        meeting_id,
        MeetingStatus.PUBLISHED.value,
        audio_key=key,
        purge_at=datetime.now(UTC) - timedelta(days=1),
    )

    class RefusingStore(InMemoryStorageProvider):
        async def delete(self, *, key: str) -> None:
            from app.storage import StorageError

            message = "the store refused"
            raise StorageError(message)

    refusing = RefusingStore(objects=dict(api.storage.objects))
    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        purged = await meetings_service.purge_due_audio(session, storage=refusing)

    assert purged == []

    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        status = await connection.scalar(
            text("SELECT status FROM meetings WHERE id = CAST(:id AS uuid)"),
            {"id": meeting_id},
        )
    assert status == MeetingStatus.PUBLISHED.value


async def test_the_purge_deadline_is_set_when_the_audio_lands(api: Harness) -> None:
    """The clock starts on arrival, not on processing: a queue backlog must not
    silently extend a customer's retention."""
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)

    ticket = (
        await api.client.post(
            f"{PREFIX}/meetings/{meeting_id}/finalize-local",
            json={
                "size_bytes": 6_000_000,
                "sha256": uuid.uuid4().hex + uuid.uuid4().hex,
                "duration_seconds": 600,
            },
            headers=headers,
        )
    ).json()
    await api.client.post(
        f"{PREFIX}/meetings/{meeting_id}/finalize",
        json={
            "upload_id": ticket["upload_id"],
            "parts": [{"part_number": p["part_number"], "etag": "e"} for p in ticket["parts"]],
        },
        headers=headers,
    )

    factory = create_session_factory(api.engine)
    async with factory() as session:
        await set_current_organization(session, uuid.UUID(organization_id))
        meeting = await session.get(Meeting, uuid.UUID(meeting_id))

    assert meeting is not None
    assert meeting.purge_at is not None
    # The default retention is thirty days (ADR-06).
    assert meeting.purge_at > datetime.now(UTC) + timedelta(days=29)


# --------------------------------------------------------------------------
# Reading a finished meeting
# --------------------------------------------------------------------------


async def test_the_detail_endpoint_answers_before_anything_is_processed(
    api: Harness,
) -> None:
    """A meeting with no report is a normal answer, not a 404."""
    _, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)

    response = await api.client.get(f"{PREFIX}/meetings/{meeting_id}/report", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"] is None
    assert body["segments"] == []
    assert body["audio_url"] is None


async def test_a_purged_meeting_has_no_audio_link(api: Harness) -> None:
    """Null there is a normal answer once the recording is gone (ADR-06)."""
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    await _force_status(api, organization_id, meeting_id, MeetingStatus.AUDIO_PURGED.value)

    response = await api.client.get(f"{PREFIX}/meetings/{meeting_id}/report", headers=headers)

    assert response.status_code == 200
    assert response.json()["audio_url"] is None


async def test_another_tenant_cannot_read_the_report(api: Harness) -> None:
    _, first = await _owner(api.client)
    _, second = await _owner(api.client)
    meeting_id = await _declare(api.client, first)

    response = await api.client.get(f"{PREFIX}/meetings/{meeting_id}/report", headers=second)

    assert response.status_code == 404


# --------------------------------------------------------------------------
# ADR-06 applies to the meetings that never make it
#
# The sweep looked at PUBLISHED and nothing else, so a recording that failed,
# was cancelled or was deleted stayed in the bucket for ever - the meetings
# least likely to be looked at again, and most likely to be forgotten.
# --------------------------------------------------------------------------


async def test_a_failed_meeting_loses_its_audio_once_the_retention_runs_out(
    api: Harness,
) -> None:
    """The retention window doubles as the retry window.

    Past it a retry could not work anyway, so keeping the recording would only
    keep the cost and the exposure.
    """
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    key = f"org/{organization_id}/meetings/{meeting_id}/audio.ogg"
    api.storage.objects[key] = 1024
    await _force_status(
        api,
        organization_id,
        meeting_id,
        MeetingStatus.FAILED.value,
        audio_key=key,
        purge_at=datetime.now(UTC) - timedelta(days=1),
    )

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        purged = await meetings_service.purge_due_audio(session, storage=api.storage)

    assert [str(identifier) for identifier in purged] == [meeting_id]
    assert key not in api.storage.objects

    # Still FAILED: nothing about the failure changed, only the recording went.
    detail = await api.client.get(f"{PREFIX}/meetings/{meeting_id}", headers=headers)
    assert detail.json()["status"] == "FAILED"


async def test_a_failed_meeting_keeps_its_audio_inside_the_retention(api: Harness) -> None:
    """Otherwise the Retry button would be a button that cannot work."""
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    key = f"org/{organization_id}/meetings/{meeting_id}/audio.ogg"
    api.storage.objects[key] = 1024
    await _force_status(
        api,
        organization_id,
        meeting_id,
        MeetingStatus.FAILED.value,
        audio_key=key,
        purge_at=datetime.now(UTC) + timedelta(days=10),
    )

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        purged = await meetings_service.purge_due_audio(session, storage=api.storage)

    assert purged == []
    assert key in api.storage.objects


async def test_a_cancelled_meeting_loses_its_audio_without_waiting(api: Harness) -> None:
    """Nothing can be done with it, so there is nothing to wait for."""
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    key = f"org/{organization_id}/meetings/{meeting_id}/audio.ogg"
    api.storage.objects[key] = 1024
    await _force_status(
        api, organization_id, meeting_id, MeetingStatus.CANCELLED.value, audio_key=key
    )

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        purged = await meetings_service.purge_due_audio(session, storage=api.storage)

    assert [str(identifier) for identifier in purged] == [meeting_id]
    assert key not in api.storage.objects


async def test_a_deleted_meeting_loses_its_audio(api: Harness) -> None:
    """The customer asked for it to be gone, which is the strongest case there is."""
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    key = f"org/{organization_id}/meetings/{meeting_id}/audio.ogg"
    api.storage.objects[key] = 1024
    await _force_status(
        api, organization_id, meeting_id, MeetingStatus.DELETED.value, audio_key=key
    )

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        purged = await meetings_service.purge_due_audio(session, storage=api.storage)

    assert [str(identifier) for identifier in purged] == [meeting_id]
    assert key not in api.storage.objects


async def test_the_sweep_aborts_an_upload_nobody_ever_finished(api: Harness) -> None:
    """Parts with no object.

    A recording abandoned between finalize-local and finalize has no object to
    delete - only parts, which the store bills for and which nothing lists back
    to us. This is the last net under the best-effort abort in cancel.
    """
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    key = f"org/{organization_id}/meetings/{meeting_id}/audio.ogg"
    upload = await api.storage.start_multipart(key=key, size_bytes=8 * 1024 * 1024)
    assert upload.upload_id in api.storage.uploads

    await _force_status(
        api,
        organization_id,
        meeting_id,
        MeetingStatus.CANCELLED.value,
        audio_key=key,
        audio_upload_id=upload.upload_id,
    )

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        purged = await meetings_service.purge_due_audio(session, storage=api.storage)

    assert [str(identifier) for identifier in purged] == [meeting_id]
    assert upload.upload_id not in api.storage.uploads


async def test_the_sweep_ignores_meetings_that_hold_nothing(api: Harness) -> None:
    """A cancelled meeting with no recording is not work."""
    organization_id, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)
    await _force_status(api, organization_id, meeting_id, MeetingStatus.CANCELLED.value)

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        purged = await meetings_service.purge_due_audio(session, storage=api.storage)

    assert purged == []


async def test_cancelling_a_recording_aborts_its_upload(api: Harness) -> None:
    """cancel took a storage provider for a long time and never used it.

    An abandoned multipart upload is billed until somebody aborts it, and the
    store will not name the open ones for us later.
    """
    _, headers = await _owner(api.client)
    meeting_id = await _declare(api.client, headers)

    slots = await api.client.post(
        f"{PREFIX}/meetings/{meeting_id}/finalize-local",
        json={
            "size_bytes": 8 * 1024 * 1024,
            "sha256": "0" * 64,
            "duration_seconds": 600,
            "paused_seconds": 0,
        },
        headers=headers,
    )
    assert slots.status_code == 200, slots.text
    upload_id = slots.json()["upload_id"]
    assert upload_id in api.storage.uploads

    cancelled = await api.client.post(f"{PREFIX}/meetings/{meeting_id}/cancel", headers=headers)

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    assert upload_id not in api.storage.uploads, "the parts are still open in the store"
