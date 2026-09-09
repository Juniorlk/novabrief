"""Meetings and the section 11 state machine (lot L2.1).

Two things are worth proving here. The state machine has to refuse everything
the diagram does not draw — including, especially, a status arriving in a
request body. And a meeting must be as invisible to a neighbouring tenant as
one that never existed.
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

APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)

PREFIX = "/api/v1"
PASSWORD = "un mot de passe long"

pytestmark = pytest.mark.asyncio


@dataclass
class Harness:
    """The API, the mailbox and the database."""

    client: AsyncClient
    mailbox: RecordingProvider
    engine: AsyncEngine


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
    app = create_app(limiter=InMemoryRateLimiter(), email_provider=mailbox)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        yield Harness(client=http, mailbox=mailbox, engine=engine)

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
