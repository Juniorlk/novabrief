"""Quota and the consumption ledger (lot L2.3).

The test that matters most here is the concurrent one. A quota check that reads
the counter, decides, and then writes looks correct in every single-threaded
test and lets two simultaneous publications each grant themselves the last
hour. It is checked against a real PostgreSQL, with two connections, because
that race cannot be reproduced against a double.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import create_session_factory, set_current_organization
from app.email import RecordingProvider
from app.main import create_app
from app.models import Meeting, Organization, UsageEntry
from app.ratelimit import InMemoryRateLimiter
from app.services import usage
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
    """The API, the object store and the database."""

    client: AsyncClient
    engine: AsyncEngine
    storage: InMemoryStorageProvider


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
    app = create_app(
        limiter=InMemoryRateLimiter(),
        email_provider=RecordingProvider(),
        storage=storage,
    )
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        yield Harness(client=http, engine=engine, storage=storage)

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


async def _set_quota(harness: Harness, organization_id: str, *, seconds: int | None) -> None:
    """Stand in for lot L5, which will write this from the `plans` table."""
    async with harness.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        await connection.execute(
            text("UPDATE organizations SET quota_seconds = :quota WHERE id = CAST(:org AS uuid)"),
            {"quota": seconds, "org": organization_id},
        )
        await connection.commit()


# --------------------------------------------------------------------------
# What a null quota means
# --------------------------------------------------------------------------


async def test_no_plan_assigned_holds_nothing(api: Harness) -> None:
    """Before lot L5 every organization is in this state; none may be blocked."""
    organization = Organization(id=uuid.uuid4(), name="x", quota_seconds=None)

    assert usage.remaining_seconds(organization) is None
    assert usage.can_afford(organization, 10_000_000)


async def test_a_quota_of_zero_affords_nothing(api: Harness) -> None:
    """The fact a null must not be confused with."""
    organization = Organization(id=uuid.uuid4(), name="x", quota_seconds=0, consumed_seconds=0)

    assert usage.remaining_seconds(organization) == 0
    assert not usage.can_afford(organization, 1)


async def test_remaining_never_goes_negative(api: Harness) -> None:
    """A pack bought mid-cycle must not display as minus two hours."""
    organization = Organization(id=uuid.uuid4(), name="x", quota_seconds=100, consumed_seconds=250)

    assert usage.remaining_seconds(organization) == 0


# --------------------------------------------------------------------------
# The atomic decrement
# --------------------------------------------------------------------------


async def test_consuming_within_the_quota_succeeds(api: Harness) -> None:
    organization_id, _ = await _owner(api.client)
    await _set_quota(api, organization_id, seconds=3600)

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        total = await usage.consume(
            session, organization_id=uuid.UUID(organization_id), seconds=1800
        )

    assert total == 1800


async def test_consuming_beyond_the_quota_is_refused(api: Harness) -> None:
    organization_id, _ = await _owner(api.client)
    await _set_quota(api, organization_id, seconds=1000)

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        with pytest.raises(usage.QuotaExceededError) as raised:
            await usage.consume(session, organization_id=uuid.UUID(organization_id), seconds=1001)

    assert raised.value.remaining == 1000


async def test_two_publications_cannot_both_take_the_last_hour(api: Harness) -> None:
    """The race a read-then-write check cannot survive.

    Both transactions see 0 consumed against a 3600 s quota and both want
    3600 s. Exactly one must win. Run against a real PostgreSQL on two
    connections, because that is the only place the race exists.
    """
    organization_id, _ = await _owner(api.client)
    await _set_quota(api, organization_id, seconds=3600)
    factory = create_session_factory(api.engine)

    async def take() -> bool:
        async with factory() as session, session.begin():
            await set_current_organization(session, uuid.UUID(organization_id))
            try:
                await usage.consume(
                    session, organization_id=uuid.UUID(organization_id), seconds=3600
                )
            except usage.QuotaExceededError:
                return False
            return True

    outcomes = await asyncio.gather(take(), take())

    assert sum(outcomes) == 1, f"both transactions charged the quota: {outcomes}"

    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        consumed = await connection.scalar(
            text("SELECT consumed_seconds FROM organizations WHERE id = CAST(:org AS uuid)"),
            {"org": organization_id},
        )
    assert consumed == 3600


async def test_a_null_quota_lets_consumption_through(api: Harness) -> None:
    """Consumption is still counted; it is simply not capped."""
    organization_id, _ = await _owner(api.client)

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        total = await usage.consume(
            session, organization_id=uuid.UUID(organization_id), seconds=7200
        )

    assert total == 7200


async def test_negative_consumption_is_rejected(api: Harness) -> None:
    """A refund is a business decision, not a negative charge slipped through."""
    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        with pytest.raises(ValueError, match="negative"):
            await usage.consume(session, organization_id=uuid.uuid4(), seconds=-60)


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


async def test_the_ledger_records_what_a_meeting_cost(api: Harness) -> None:
    """ADR-08: the unit-economics dashboard reads these rows."""
    organization_id, headers = await _owner(api.client)
    declared = await api.client.post(
        f"{PREFIX}/meetings",
        json={"started_at": "2026-09-09T10:00:00Z"},
        headers=headers,
    )
    meeting_id = declared.json()["id"]

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        organization = await session.get(Organization, uuid.UUID(organization_id))
        meeting = await session.get(Meeting, uuid.UUID(meeting_id))
        assert organization is not None
        assert meeting is not None
        await usage.record(
            session,
            organization=organization,
            meeting=meeting,
            seconds_billed=1800,
            stt_provider="assemblyai",
            stt_cost_usd=Decimal("0.10500"),
            llm_provider="openai",
            llm_tokens_in=4000,
            llm_tokens_out=900,
            llm_cost_usd=Decimal("0.00340"),
        )

    async with factory() as session:
        await set_current_organization(session, uuid.UUID(organization_id))
        entry = (await session.scalars(select(UsageEntry))).first()

    assert entry is not None
    assert entry.seconds_billed == 1800
    # Decimal, not float: a cost summed over thousands of meetings must not drift.
    assert entry.stt_cost_usd == Decimal("0.10500")
    assert entry.llm_tokens_in == 4000


async def test_the_ledger_survives_the_meeting_it_describes(api: Harness) -> None:
    """Section 19.2: the meeting goes, the accounting record stays."""
    organization_id, headers = await _owner(api.client)
    declared = await api.client.post(
        f"{PREFIX}/meetings", json={"started_at": "2026-09-09T10:00:00Z"}, headers=headers
    )
    meeting_id = declared.json()["id"]

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        organization = await session.get(Organization, uuid.UUID(organization_id))
        meeting = await session.get(Meeting, uuid.UUID(meeting_id))
        assert organization is not None and meeting is not None
        await usage.record(session, organization=organization, meeting=meeting, seconds_billed=600)

    # A hard delete, as the audit purge would do.
    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        await connection.execute(
            text("DELETE FROM meetings WHERE id = CAST(:id AS uuid)"), {"id": meeting_id}
        )
        await connection.commit()

        rows = await connection.execute(
            text(
                "SELECT meeting_id, seconds_billed FROM usage_ledger "
                "WHERE organization_id = CAST(:org AS uuid)"
            ),
            {"org": organization_id},
        )
        surviving = rows.all()

    assert len(surviving) == 1
    assert surviving[0].meeting_id is None
    assert surviving[0].seconds_billed == 600


async def test_purging_an_organization_detaches_its_ledger_rather_than_blocking(
    api: Harness,
) -> None:
    """The contradiction in the specification, resolved and pinned down.

    The reference DDL puts ON DELETE RESTRICT on the organization, which would
    make EF-06's promise ("nothing survives a deletion") impossible to keep.
    Releasing the reference keeps both: the aggregate cost history stays for
    Novafrik, and the row no longer names the customer.
    """
    organization_id, headers = await _owner(api.client)
    declared = await api.client.post(
        f"{PREFIX}/meetings", json={"started_at": "2026-09-09T10:00:00Z"}, headers=headers
    )

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, uuid.UUID(organization_id))
        organization = await session.get(Organization, uuid.UUID(organization_id))
        meeting = await session.get(Meeting, uuid.UUID(declared.json()["id"]))
        assert organization is not None and meeting is not None
        await usage.record(session, organization=organization, meeting=meeting, seconds_billed=900)

    admin_url = os.environ.get(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql+asyncpg://novabrief:novabrief@localhost:5432/novabrief",
    )
    admin = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with admin.connect() as connection:
            # The deletion must not be blocked by the ledger.
            await connection.execute(
                text("DELETE FROM organizations WHERE id = CAST(:org AS uuid)"),
                {"org": organization_id},
            )
            await connection.commit()

            still_named = await connection.scalar(
                text(
                    "SELECT count(*) FROM usage_ledger WHERE organization_id = CAST(:org AS uuid)"
                ),
                {"org": organization_id},
            )
            detached = await connection.scalar(
                text(
                    "SELECT count(*) FROM usage_ledger "
                    "WHERE organization_id IS NULL AND seconds_billed = 900"
                )
            )
    finally:
        await admin.dispose()

    assert still_named == 0, "the ledger still names a deleted organization"
    assert detached >= 1, "the cost history was destroyed with the organization"


# --------------------------------------------------------------------------
# QUOTA_HOLD at finalisation
# --------------------------------------------------------------------------


async def test_a_meeting_over_quota_is_held_not_refused(api: Harness) -> None:
    """Section 20.3: going over blocks processing, never the recording."""
    organization_id, headers = await _owner(api.client)
    await _set_quota(api, organization_id, seconds=60)

    declared = await api.client.post(
        f"{PREFIX}/meetings", json={"started_at": "2026-09-09T10:00:00Z"}, headers=headers
    )
    meeting_id = declared.json()["id"]

    ticket = await api.client.post(
        f"{PREFIX}/meetings/{meeting_id}/finalize-local",
        json={
            "size_bytes": 6_000_000,
            "sha256": uuid.uuid4().hex + uuid.uuid4().hex,
            "duration_seconds": 3600,
        },
        headers=headers,
    )
    assert ticket.status_code == 200, ticket.text
    body = ticket.json()

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting_id}/finalize",
        json={
            "upload_id": body["upload_id"],
            "parts": [{"part_number": p["part_number"], "etag": "e"} for p in body["parts"]],
        },
        headers=headers,
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "QUOTA_HOLD"


async def test_a_held_meeting_keeps_its_audio(api: Harness) -> None:
    """The one failure a customer cannot recover from is a lost recording."""
    organization_id, headers = await _owner(api.client)
    await _set_quota(api, organization_id, seconds=60)
    declared = await api.client.post(
        f"{PREFIX}/meetings", json={"started_at": "2026-09-09T10:00:00Z"}, headers=headers
    )
    meeting_id = declared.json()["id"]
    ticket = (
        await api.client.post(
            f"{PREFIX}/meetings/{meeting_id}/finalize-local",
            json={
                "size_bytes": 6_000_000,
                "sha256": uuid.uuid4().hex + uuid.uuid4().hex,
                "duration_seconds": 3600,
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

    assert api.storage.objects, "the audio was dropped when the quota ran out"


async def test_a_meeting_within_quota_is_queued(api: Harness) -> None:
    organization_id, headers = await _owner(api.client)
    await _set_quota(api, organization_id, seconds=7200)
    declared = await api.client.post(
        f"{PREFIX}/meetings", json={"started_at": "2026-09-09T10:00:00Z"}, headers=headers
    )
    meeting_id = declared.json()["id"]
    ticket = (
        await api.client.post(
            f"{PREFIX}/meetings/{meeting_id}/finalize-local",
            json={
                "size_bytes": 6_000_000,
                "sha256": uuid.uuid4().hex + uuid.uuid4().hex,
                "duration_seconds": 3600,
            },
            headers=headers,
        )
    ).json()

    response = await api.client.post(
        f"{PREFIX}/meetings/{meeting_id}/finalize",
        json={
            "upload_id": ticket["upload_id"],
            "parts": [{"part_number": p["part_number"], "etag": "e"} for p in ticket["parts"]],
        },
        headers=headers,
    )

    assert response.json()["status"] == "QUEUED"
