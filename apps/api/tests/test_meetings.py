"""Meetings and the section 11 state machine (lot L2.1).

Two things are worth proving here. The state machine has to refuse everything
the diagram does not draw — including, especially, a status arriving in a
request body. And a meeting must be as invisible to a neighbouring tenant as
one that never existed.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import create_session_factory
from app.email import RecordingProvider
from app.main import create_app
from app.models import Meeting, MeetingStatus
from app.ratelimit import InMemoryRateLimiter
from app.services import meetings
from app.storage import InMemoryStorageProvider

APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)

PREFIX = "/api/v1"
PASSWORD = "un mot de passe long"

pytestmark = pytest.mark.asyncio


@dataclass
class Harness:
    """The API, the mailbox, the object store and the database."""

    client: AsyncClient
    mailbox: RecordingProvider
    engine: AsyncEngine
    storage: InMemoryStorageProvider
    dispatched: list[tuple[uuid.UUID, uuid.UUID]]


@pytest_asyncio.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    """A running API against the test database."""
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

    mailbox = RecordingProvider()
    # The store is passed explicitly rather than left to the configuration: a
    # .env that happens to hold real R2 credentials must never turn a test run
    # into writes against the production bucket.
    storage = InMemoryStorageProvider()
    # A recorder, not the real queue: a unit test must not need a broker, and
    # this is also how a test can see what would have been dispatched.
    dispatched: list[tuple[uuid.UUID, uuid.UUID]] = []

    def record(*, organization_id: uuid.UUID, meeting_id: uuid.UUID) -> None:
        dispatched.append((organization_id, meeting_id))

    app = create_app(
        limiter=InMemoryRateLimiter(),
        email_provider=mailbox,
        storage=storage,
        dispatch=record,
    )
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        yield Harness(
            client=http,
            mailbox=mailbox,
            engine=engine,
            storage=storage,
            dispatched=dispatched,
        )

    await engine.dispose()
    get_settings.cache_clear()


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@test.cm"


async def _owner(client: AsyncClient) -> dict[str, str]:
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
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _colleague(
    client: AsyncClient,
    mailbox: RecordingProvider,
    owner_headers: dict[str, str],
    *,
    role: str = "MEMBER",
) -> dict[str, str]:
    """A second person inside the same organization.

    Needed because the interesting privacy failures are between colleagues:
    RLS already keeps other tenants out, and a test using a second organization
    would prove nothing about `is_private`.
    """
    invitee = _email()
    invited = await client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": invitee, "role": role},
        headers=owner_headers,
    )
    assert invited.status_code < 300, invited.text

    message = mailbox.last_to(invitee)
    assert message is not None
    match = re.search(r"token=([A-Za-z0-9_\-]+)", message.text)
    assert match, message.text

    accepted = await client.post(
        f"{PREFIX}/auth/invitations/accept",
        json={"token": match.group(1), "full_name": "Colleague", "password": PASSWORD},
    )
    assert accepted.status_code < 300, accepted.text

    signed_in = await client.post(
        f"{PREFIX}/auth/token", json={"email": invitee, "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return {"Authorization": f"Bearer {signed_in.json()['access_token']}"}


async def _declare(client: AsyncClient, headers: dict[str, str], **extra: object) -> dict[str, str]:
    payload: dict[str, object] = {"started_at": datetime.now(UTC).isoformat()}
    payload.update(extra)
    response = await client.post(f"{PREFIX}/meetings", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, str] = response.json()
    return body


# --------------------------------------------------------------------------
# The state machine, on its own
# --------------------------------------------------------------------------


async def test_every_state_appears_in_the_transition_table() -> None:
    """A state nobody can leave *or* reach is a state that was forgotten."""
    for state in MeetingStatus:
        assert state in meetings.TRANSITIONS, f"{state} has no row"

    reachable = {target for targets in meetings.TRANSITIONS.values() for target in targets}
    unreachable = set(MeetingStatus) - reachable - {MeetingStatus.CREATED}
    assert not unreachable, f"unreachable states: {unreachable}"


async def test_the_terminal_states_lead_nowhere() -> None:
    assert meetings.TRANSITIONS[MeetingStatus.CANCELLED] == frozenset()
    assert meetings.TRANSITIONS[MeetingStatus.DELETED] == frozenset()


async def test_a_failed_meeting_can_be_retried() -> None:
    """EF-45 promises a Retry button; the table has to allow it."""
    assert MeetingStatus.QUEUED in meetings.TRANSITIONS[MeetingStatus.FAILED]


async def test_the_queue_cannot_jump_straight_to_completed() -> None:
    """The shortcut that would let a worker fake a result."""
    assert MeetingStatus.COMPLETED not in meetings.TRANSITIONS[MeetingStatus.QUEUED]
    assert MeetingStatus.PUBLISHED not in meetings.TRANSITIONS[MeetingStatus.QUEUED]


async def test_audio_is_only_purged_after_publication() -> None:
    """ADR-06: the audio goes, the text stays — and not before the report exists."""
    for state in MeetingStatus:
        if state is MeetingStatus.PUBLISHED:
            continue
        assert MeetingStatus.AUDIO_PURGED not in meetings.TRANSITIONS[state], (
            f"{state} must not reach AUDIO_PURGED"
        )


# --------------------------------------------------------------------------
# Declaring
# --------------------------------------------------------------------------


async def test_declaring_a_meeting_returns_a_debug_id(api: Harness) -> None:
    """ADR-07: it exists from the first instant, before anything can go wrong."""
    headers = await _owner(api.client)

    meeting = await _declare(api.client, headers, title="Comite du lundi")

    assert meeting["status"] == "CREATED"
    assert meeting["debug_id"].startswith("DBG-MTG-")
    assert meeting["title"] == "Comite du lundi"


async def test_a_meeting_needs_no_title(api: Harness) -> None:
    """Recording starts before anyone has named the thing."""
    headers = await _owner(api.client)

    meeting = await _declare(api.client, headers)

    assert meeting["title"] is None


async def test_the_client_cannot_declare_a_status(api: Harness) -> None:
    """The one request that would make the state machine decorative."""
    headers = await _owner(api.client)

    response = await api.client.post(
        f"{PREFIX}/meetings",
        json={"started_at": datetime.now(UTC).isoformat(), "status": "COMPLETED"},
        headers=headers,
    )

    assert response.status_code == 422


async def test_declaring_needs_a_token(api: Harness) -> None:
    response = await api.client.post(
        f"{PREFIX}/meetings", json={"started_at": datetime.now(UTC).isoformat()}
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


async def test_meetings_are_listed_newest_first(api: Harness) -> None:
    headers = await _owner(api.client)
    older = await _declare(api.client, headers, title="Ancienne")
    newer = await _declare(api.client, headers, title="Recente")

    listing = (await api.client.get(f"{PREFIX}/meetings", headers=headers)).json()

    assert [row["id"] for row in listing][:2] == [newer["id"], older["id"]]


async def test_the_listing_can_be_filtered_by_status(api: Harness) -> None:
    headers = await _owner(api.client)
    await _declare(api.client, headers)

    listing = (await api.client.get(f"{PREFIX}/meetings?status=QUEUED", headers=headers)).json()

    assert listing == []


async def test_an_unknown_status_filter_is_refused(api: Harness) -> None:
    headers = await _owner(api.client)

    response = await api.client.get(f"{PREFIX}/meetings?status=NONSENSE", headers=headers)

    assert response.status_code == 422


async def test_a_meeting_from_another_organization_is_not_found(api: Harness) -> None:
    """404 rather than 403: confirming the identifier exists would leak it."""
    first = await _owner(api.client)
    second = await _owner(api.client)
    meeting = await _declare(api.client, first)

    response = await api.client.get(f"{PREFIX}/meetings/{meeting['id']}", headers=second)

    assert response.status_code == 404
    assert response.json()["code"] == "MEETING_NOT_FOUND"


async def test_a_neighbouring_organization_sees_an_empty_list(api: Harness) -> None:
    first = await _owner(api.client)
    second = await _owner(api.client)
    await _declare(api.client, first)

    listing = (await api.client.get(f"{PREFIX}/meetings", headers=second)).json()

    assert listing == []


# --------------------------------------------------------------------------
# Editing and deleting
# --------------------------------------------------------------------------


async def test_the_author_can_rename_a_meeting(api: Harness) -> None:
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)

    response = await api.client.patch(
        f"{PREFIX}/meetings/{meeting['id']}", json={"title": "Renomme"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Renomme"


async def test_editing_cannot_reach_the_status(api: Harness) -> None:
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)

    response = await api.client.patch(
        f"{PREFIX}/meetings/{meeting['id']}", json={"status": "PUBLISHED"}, headers=headers
    )

    assert response.status_code == 422


async def test_deleting_leaves_the_row_for_the_accounting(api: Harness) -> None:
    """Section 19.2: the consumption record needs a meeting to point at."""
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)

    response = await api.client.delete(f"{PREFIX}/meetings/{meeting['id']}", headers=headers)

    assert response.status_code == 204
    assert (
        await api.client.get(f"{PREFIX}/meetings/{meeting['id']}", headers=headers)
    ).status_code == 404

    organization_id = (await api.client.get(f"{PREFIX}/me", headers=headers)).json()[
        "organization"
    ]["id"]
    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        row_status = await connection.scalar(
            text("SELECT status FROM meetings WHERE id = CAST(:id AS uuid)"),
            {"id": meeting["id"]},
        )
    assert row_status == "DELETED"


async def test_a_deleted_meeting_stays_out_of_the_listing(api: Harness) -> None:
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)
    await api.client.delete(f"{PREFIX}/meetings/{meeting['id']}", headers=headers)

    listing = (await api.client.get(f"{PREFIX}/meetings", headers=headers)).json()

    assert meeting["id"] not in [row["id"] for row in listing]


# --------------------------------------------------------------------------
# advance() is the only door
# --------------------------------------------------------------------------


async def test_an_illegal_transition_is_refused(api: Harness) -> None:
    """A worker must not be able to skip the queue."""
    headers = await _owner(api.client)
    declared = await _declare(api.client, headers)
    organization_id = (await api.client.get(f"{PREFIX}/me", headers=headers)).json()[
        "organization"
    ]["id"]

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        meeting = await session.get(Meeting, uuid.UUID(declared["id"]))
        assert meeting is not None

        with pytest.raises(meetings.IllegalTransitionError):
            await meetings.advance(session, meeting=meeting, to=MeetingStatus.COMPLETED)


async def test_a_legal_transition_is_audited(api: Harness) -> None:
    """Section 11 asks for a timestamp and an actor on every move."""
    headers = await _owner(api.client)
    declared = await _declare(api.client, headers)
    organization_id = (await api.client.get(f"{PREFIX}/me", headers=headers)).json()[
        "organization"
    ]["id"]

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        meeting = await session.get(Meeting, uuid.UUID(declared["id"]))
        assert meeting is not None
        await meetings.advance(session, meeting=meeting, to=MeetingStatus.UPLOADING)

    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        row = (
            await connection.execute(
                text(
                    "SELECT actor_type, metadata::text AS payload FROM audit_log "
                    "WHERE action = 'meeting.transition' ORDER BY id DESC LIMIT 1"
                )
            )
        ).one()

    # No actor: the workers act as the system, not on anybody's behalf.
    assert row.actor_type == "SYSTEM"
    assert "CREATED" in row.payload
    assert "UPLOADING" in row.payload


async def test_completion_stamps_the_time(api: Harness) -> None:
    headers = await _owner(api.client)
    declared = await _declare(api.client, headers)
    organization_id = (await api.client.get(f"{PREFIX}/me", headers=headers)).json()[
        "organization"
    ]["id"]
    before = datetime.now(UTC) - timedelta(seconds=1)

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        meeting = await session.get(Meeting, uuid.UUID(declared["id"]))
        assert meeting is not None
        for step in (
            MeetingStatus.UPLOADING,
            MeetingStatus.QUEUED,
            MeetingStatus.TRANSCRIBING,
            MeetingStatus.ANALYZING,
            MeetingStatus.COMPLETED,
        ):
            await meetings.advance(session, meeting=meeting, to=step)
        completed_at = meeting.completed_at

    assert completed_at is not None
    assert completed_at >= before


# --------------------------------------------------------------------------
# Upload and finalisation (L2.2)
# --------------------------------------------------------------------------


def _sha256() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


async def _ticket(
    api: Harness, headers: dict[str, str], meeting_id: str, *, size_bytes: int = 6_000_000
) -> dict[str, object]:
    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting_id}/finalize-local",
        json={
            "size_bytes": size_bytes,
            "sha256": _sha256(),
            "duration_seconds": 1800,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def test_finalize_local_returns_upload_slots(api: Harness) -> None:
    """Section 16.4: the client uploads straight to the bucket, never through us."""
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)

    ticket = await _ticket(api, headers, meeting["id"])

    assert ticket["upload_id"]
    assert len(ticket["parts"]) >= 1  # type: ignore[arg-type]
    assert ticket["expires_in_seconds"] == 900


async def test_finalize_local_moves_the_meeting_to_uploading(api: Harness) -> None:
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)
    await _ticket(api, headers, meeting["id"])

    current = (await api.client.get(f"{PREFIX}/meetings/{meeting['id']}", headers=headers)).json()

    assert current["status"] == "UPLOADING"


async def test_a_declared_size_of_zero_is_refused(api: Harness) -> None:
    """A meeting with no audio is a bug upstream, not a zero-byte object."""
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize-local",
        json={"size_bytes": 0, "sha256": _sha256(), "duration_seconds": 10},
        headers=headers,
    )

    assert response.status_code == 422


async def test_a_malformed_digest_is_refused(api: Harness) -> None:
    """The digest is checked at the boundary, not months later by a worker."""
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize-local",
        json={"size_bytes": 1024, "sha256": "not-a-digest", "duration_seconds": 10},
        headers=headers,
    )

    assert response.status_code == 422


async def test_only_the_author_may_upload(api: Harness) -> None:
    """A colleague has no business attaching audio to someone else's recording."""
    owner = await _owner(api.client)
    meeting = await _declare(api.client, owner)
    other = await _owner(api.client)

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize-local",
        json={"size_bytes": 1024, "sha256": _sha256(), "duration_seconds": 10},
        headers=other,
    )

    # Another tenant cannot even see it.
    assert response.status_code == 404


async def test_starting_an_upload_twice_is_refused(api: Harness) -> None:
    """A replayed request must not reopen a recording that already moved on."""
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)
    await _ticket(api, headers, meeting["id"])

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize-local",
        json={"size_bytes": 6_000_000, "sha256": _sha256(), "duration_seconds": 10},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ILLEGAL_TRANSITION"


async def test_finalizing_queues_the_meeting(api: Harness) -> None:
    """EF-40: 202, and the meeting is in the queue."""
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)
    ticket = await _ticket(api, headers, meeting["id"])
    parts = [
        {"part_number": part["part_number"], "etag": "e"}  # type: ignore[index]
        for part in ticket["parts"]  # type: ignore[union-attr]
    ]

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize",
        json={"upload_id": ticket["upload_id"], "parts": parts, "client_version": "0.1.0"},
        headers=headers,
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "QUEUED"


async def test_finalizing_a_short_upload_is_refused(api: Harness) -> None:
    """A truncated recording would become a confident, incomplete report."""
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)
    ticket = await _ticket(api, headers, meeting["id"], size_bytes=6_000_000)
    # The store ends up holding less than was promised: a dropped part, a
    # client that stopped early, a disk that filled.
    api.storage.stored_size_override = 4_000_000
    parts = [
        {"part_number": part["part_number"], "etag": "e"}  # type: ignore[index]
        for part in ticket["parts"]  # type: ignore[union-attr]
    ]

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize",
        json={"upload_id": ticket["upload_id"], "parts": parts},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "SIZE_MISMATCH"


async def test_finalizing_with_a_missing_part_is_refused(api: Harness) -> None:
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)
    ticket = await _ticket(api, headers, meeting["id"], size_bytes=11 * 1024 * 1024)

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize",
        json={"upload_id": ticket["upload_id"], "parts": [{"part_number": 1, "etag": "e"}]},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "UPLOAD_INCOMPLETE"


async def test_finalizing_without_an_upload_is_refused(api: Harness) -> None:
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize",
        json={"upload_id": "nope", "parts": [{"part_number": 1, "etag": "e"}]},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "UPLOAD_NOT_STARTED"


async def test_a_recording_can_be_abandoned(api: Harness) -> None:
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)

    response = await api.client.post(f"{PREFIX}/meetings/{meeting['id']}/cancel", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"


async def test_finalizing_hands_the_meeting_to_the_workers(api: Harness) -> None:
    """EF-40: the 202 means somebody else is now doing the work.

    Without this the meeting would sit in QUEUED for ever and the API would
    still answer 202, which is the most convincing way to be broken.
    """
    headers = await _owner(api.client)
    meeting = await _declare(api.client, headers)
    ticket = await _ticket(api, headers, meeting["id"])

    await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize",
        json={
            "upload_id": ticket["upload_id"],
            "parts": [
                {"part_number": part["part_number"], "etag": "e"}  # type: ignore[index]
                for part in ticket["parts"]  # type: ignore[union-attr]
            ],
        },
        headers=headers,
    )

    assert [str(pair[1]) for pair in api.dispatched] == [meeting["id"]]


async def test_a_meeting_held_for_quota_is_not_dispatched(api: Harness) -> None:
    """Section 11: the payment webhook releases it, not the finalisation."""
    headers = await _owner(api.client)
    organization_id = (await api.client.get(f"{PREFIX}/me", headers=headers)).json()[
        "organization"
    ]["id"]
    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        await connection.execute(
            text("UPDATE organizations SET quota_seconds = 60 WHERE id = CAST(:org AS uuid)"),
            {"org": organization_id},
        )
        await connection.commit()

    meeting = await _declare(api.client, headers)
    ticket = await _ticket(api, headers, meeting["id"])
    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting['id']}/finalize",
        json={
            "upload_id": ticket["upload_id"],
            "parts": [
                {"part_number": part["part_number"], "etag": "e"}  # type: ignore[index]
                for part in ticket["parts"]  # type: ignore[union-attr]
            ],
        },
        headers=headers,
    )

    assert response.json()["status"] == "QUOTA_HOLD"
    assert api.dispatched == []


# --------------------------------------------------------------------------
# Private meetings (EF-22, section 17.2)
#
# The field existed, was editable and was displayed long before anything
# enforced it, which is the worst possible order: a person marking a meeting
# private was told it was private and it was not. These tests exist so that
# cannot silently come back.
# --------------------------------------------------------------------------


async def test_a_colleague_does_not_see_a_private_meeting_in_the_listing(api: Harness) -> None:
    owner = await _owner(api.client)
    colleague = await _colleague(api.client, api.mailbox, owner)

    private = await _declare(api.client, owner, title="Entretien annuel", is_private=True)
    shared = await _declare(api.client, owner, title="Comite du lundi")

    listed = await api.client.get(f"{PREFIX}/meetings", headers=colleague)

    assert listed.status_code == 200, listed.text
    identifiers = {row["id"] for row in listed.json()}
    assert shared["id"] in identifiers
    assert private["id"] not in identifiers


async def test_a_colleague_reading_a_private_meeting_is_told_it_does_not_exist(
    api: Harness,
) -> None:
    """404, not 403.

    403 would confirm the meeting exists, which is exactly the fact its author
    asked us to keep. The answer is the same one another tenant gets.
    """
    owner = await _owner(api.client)
    colleague = await _colleague(api.client, api.mailbox, owner)
    private = await _declare(api.client, owner, title="Entretien annuel", is_private=True)

    response = await api.client.get(f"{PREFIX}/meetings/{private['id']}", headers=colleague)

    assert response.status_code == 404
    assert response.json()["code"] == "MEETING_NOT_FOUND"


async def test_a_colleague_cannot_reach_the_report_of_a_private_meeting(api: Harness) -> None:
    """The report route carries the presigned audio link, so it is the one that matters."""
    owner = await _owner(api.client)
    colleague = await _colleague(api.client, api.mailbox, owner)
    private = await _declare(api.client, owner, title="Entretien annuel", is_private=True)

    response = await api.client.get(f"{PREFIX}/meetings/{private['id']}/report", headers=colleague)

    assert response.status_code == 404


async def test_a_colleague_cannot_open_a_status_socket_on_a_private_meeting(api: Harness) -> None:
    """The ticket is the socket's only credential, so the gate has to be here."""
    owner = await _owner(api.client)
    colleague = await _colleague(api.client, api.mailbox, owner)
    private = await _declare(api.client, owner, title="Entretien annuel", is_private=True)

    response = await api.client.post(
        f"{PREFIX}/meetings/{private['id']}/ws-ticket", headers=colleague
    )

    assert response.status_code == 404


async def test_an_administrator_still_sees_a_private_meeting(api: Harness) -> None:
    """Section 17.2 gives it to the author *and* the administrators."""
    owner = await _owner(api.client)
    member = await _colleague(api.client, api.mailbox, owner)
    private = await _declare(api.client, member, title="Note perso", is_private=True)

    seen = await api.client.get(f"{PREFIX}/meetings/{private['id']}", headers=owner)
    listed = await api.client.get(f"{PREFIX}/meetings", headers=owner)

    assert seen.status_code == 200
    assert private["id"] in {row["id"] for row in listed.json()}


async def test_the_author_still_sees_their_own_private_meeting(api: Harness) -> None:
    owner = await _owner(api.client)
    private = await _declare(api.client, owner, title="Entretien annuel", is_private=True)

    seen = await api.client.get(f"{PREFIX}/meetings/{private['id']}", headers=owner)
    listed = await api.client.get(f"{PREFIX}/meetings", headers=owner)

    assert seen.status_code == 200
    assert private["id"] in {row["id"] for row in listed.json()}


async def test_making_a_meeting_private_hides_it_from_a_colleague(api: Harness) -> None:
    """The flag has to bite on an existing meeting, not only at declaration."""
    owner = await _owner(api.client)
    colleague = await _colleague(api.client, api.mailbox, owner)
    meeting = await _declare(api.client, owner, title="Comite du lundi")

    visible = await api.client.get(f"{PREFIX}/meetings/{meeting['id']}", headers=colleague)
    assert visible.status_code == 200

    await api.client.patch(
        f"{PREFIX}/meetings/{meeting['id']}", json={"is_private": True}, headers=owner
    )

    hidden = await api.client.get(f"{PREFIX}/meetings/{meeting['id']}", headers=colleague)
    assert hidden.status_code == 404


async def test_a_private_meeting_does_not_consume_a_page_slot(api: Harness) -> None:
    """The rule is applied in SQL, not after the LIMIT.

    Filtering in Python would return a short page: the database would hand back
    two rows, one would be dropped, and the caller would see one meeting while
    believing there were no more.
    """
    owner = await _owner(api.client)
    colleague = await _colleague(api.client, api.mailbox, owner)

    await _declare(api.client, owner, title="Prive", is_private=True)
    visible = await _declare(api.client, owner, title="Partage")

    page = await api.client.get(f"{PREFIX}/meetings?limit=1", headers=colleague)

    assert page.status_code == 200
    assert [row["id"] for row in page.json()] == [visible["id"]]
