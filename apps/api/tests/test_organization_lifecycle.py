"""Export and deletion of an organization (EF-06), over HTTP.

Two things carry real risk here and both are tested directly: an export must
never carry a credential, and a deletion must erase everything — but only after
the seven days, and only if nobody changed their mind.
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
from app.models import TENANT_TABLES
from app.ratelimit import InMemoryRateLimiter
from app.services.organizations import DELETION_RETRACTION, purge_due_organizations

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
    """A running API against the test database, with emails captured."""
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


async def _owner(client: AsyncClient) -> tuple[str, dict[str, str]]:
    """Register an organization; return the Owner's address and auth header."""
    address = _email()
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Owner",
            "organization_name": "Test Org",
            "password": PASSWORD,
            "email": address,
        },
    )
    assert response.status_code == 201, response.text
    return address, {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _member(harness: Harness, owner: dict[str, str]) -> dict[str, str]:
    """Invite, accept and sign in an ordinary MEMBER."""
    address = _email()
    invited = await harness.client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": address, "role": "MEMBER"},
        headers=owner,
    )
    assert invited.status_code == 201, invited.text

    message = harness.mailbox.last_to(address)
    assert message is not None
    match = re.search(r"token=([A-Za-z0-9_\-]+)", message.text)
    assert match, message.text

    accepted = await harness.client.post(
        f"{PREFIX}/auth/invitations/accept",
        json={"token": match.group(1), "full_name": "Member", "password": PASSWORD},
    )
    assert accepted.status_code == 204, accepted.text

    signed_in = await harness.client.post(
        f"{PREFIX}/auth/token", json={"email": address, "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return {"Authorization": f"Bearer {signed_in.json()['access_token']}"}


async def _organization_id(harness: Harness, headers: dict[str, str]) -> str:
    body = (await harness.client.get(f"{PREFIX}/me", headers=headers)).json()
    identifier: str = body["organization"]["id"]
    return identifier


async def _backdate_request(harness: Harness, organization_id: str, *, days: int) -> None:
    """Move a pending deletion into the past, to reach the purge without waiting."""
    async with harness.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        await connection.execute(
            text(
                "UPDATE organizations SET deletion_requested_at = :moment "
                "WHERE id = CAST(:org AS uuid)"
            ),
            {"moment": datetime.now(UTC) - timedelta(days=days), "org": organization_id},
        )
        await connection.commit()


async def _rows_left(harness: Harness, organization_id: str) -> dict[str, int]:
    """Count what survives for one organization, table by table.

    Counted with the owner role rather than through the API: after a purge the
    API would answer "nothing" simply because RLS shows nothing, which is not
    the same as the rows being gone.
    """
    admin_url = os.environ.get(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql+asyncpg://novabrief:novabrief@localhost:5432/novabrief",
    )
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            counts: dict[str, int] = {}
            organizations = await connection.scalar(
                text("SELECT count(*) FROM organizations WHERE id = CAST(:org AS uuid)"),
                {"org": organization_id},
            )
            counts["organizations"] = int(organizations or 0)
            for table in TENANT_TABLES:
                total = await connection.scalar(
                    text(
                        f"SELECT count(*) FROM {table} "  # noqa: S608 - table names are a constant
                        "WHERE organization_id = CAST(:org AS uuid)"
                    ),
                    {"org": organization_id},
                )
                counts[table] = int(total or 0)
            return counts
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


async def test_an_export_contains_the_organization_and_its_members(api: Harness) -> None:
    address, headers = await _owner(api.client)
    await _member(api, headers)

    response = await api.client.get(f"{PREFIX}/organizations/current/export", headers=headers)

    assert response.status_code == 200, response.text
    document = response.json()
    assert document["format"] == "novabrief.export.v1"
    assert document["organization"]["name"] == "Test Org"
    assert {member["email"] for member in document["members"]} >= {address}
    assert len(document["members"]) == 2
    assert len(document["invitations"]) == 1
    assert document["audit_log"], "an export without its audit trail is not complete"


async def test_an_export_carries_no_credential(api: Harness) -> None:
    """The file is downloaded and forwarded; a secret in it outlives every revocation."""
    _, headers = await _owner(api.client)
    await _member(api, headers)

    response = await api.client.get(f"{PREFIX}/organizations/current/export", headers=headers)

    body = response.text
    assert "password_hash" not in body
    assert "token_hash" not in body
    assert "totp" not in body.lower()
    # Argon2 hashes start with this marker; the plain password must not be there
    # either, in any form.
    assert "$argon2" not in body
    assert PASSWORD not in body


async def test_an_export_is_served_as_a_download(api: Harness) -> None:
    _, headers = await _owner(api.client)

    response = await api.client.get(f"{PREFIX}/organizations/current/export", headers=headers)

    assert "attachment" in response.headers["content-disposition"]
    assert ".json" in response.headers["content-disposition"]


async def test_an_export_never_crosses_tenants(api: Harness) -> None:
    """The strongest statement RLS can make: two organizations, two exports."""
    _, first = await _owner(api.client)
    second_address, _ = await _owner(api.client)

    document = (
        await api.client.get(f"{PREFIX}/organizations/current/export", headers=first)
    ).json()

    assert second_address not in {member["email"] for member in document["members"]}
    assert len(document["members"]) == 1


async def test_an_ordinary_member_cannot_export(api: Harness) -> None:
    _, owner = await _owner(api.client)
    member = await _member(api, owner)

    response = await api.client.get(f"{PREFIX}/organizations/current/export", headers=member)

    assert response.status_code == 403


# --------------------------------------------------------------------------
# Deletion request and retraction
# --------------------------------------------------------------------------


async def test_an_owner_schedules_a_deletion(api: Harness) -> None:
    address, headers = await _owner(api.client)

    response = await api.client.delete(f"{PREFIX}/organizations/current", headers=headers)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["retraction_days"] == DELETION_RETRACTION.days
    requested = datetime.fromisoformat(body["deletion_requested_at"])
    purge_after = datetime.fromisoformat(body["purge_after"])
    assert purge_after - requested == DELETION_RETRACTION

    # The Owner is told, which is how a hijacked account gets noticed in time.
    assert api.mailbox.last_to(address) is not None


async def test_nothing_is_destroyed_when_the_deletion_is_requested(api: Harness) -> None:
    """202 means accepted, not done. The data has to still be there."""
    _, headers = await _owner(api.client)
    organization_id = await _organization_id(api, headers)

    await api.client.delete(f"{PREFIX}/organizations/current", headers=headers)

    counts = await _rows_left(api, organization_id)
    assert counts["organizations"] == 1
    assert counts["users"] == 1


async def test_a_pending_deletion_is_visible_on_the_profile(api: Harness) -> None:
    """The web app needs it to show the banner and the countdown."""
    _, headers = await _owner(api.client)
    await api.client.delete(f"{PREFIX}/organizations/current", headers=headers)

    body = (await api.client.get(f"{PREFIX}/me", headers=headers)).json()

    assert body["organization"]["deletion_requested_at"] is not None


async def test_an_admin_cannot_schedule_a_deletion(api: Harness) -> None:
    """EF-06 names the Owner. An Admin may run the organization, not end it."""
    _, owner = await _owner(api.client)
    address = _email()
    invited = await api.client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": address, "role": "ADMIN"},
        headers=owner,
    )
    assert invited.status_code == 201
    message = api.mailbox.last_to(address)
    assert message is not None
    match = re.search(r"token=([A-Za-z0-9_\-]+)", message.text)
    assert match
    await api.client.post(
        f"{PREFIX}/auth/invitations/accept",
        json={"token": match.group(1), "full_name": "Admin", "password": PASSWORD},
    )
    signed_in = await api.client.post(
        f"{PREFIX}/auth/token", json={"email": address, "password": PASSWORD}
    )
    admin = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}

    response = await api.client.delete(f"{PREFIX}/organizations/current", headers=admin)

    assert response.status_code == 403


async def test_asking_twice_does_not_restart_the_clock(api: Harness) -> None:
    """Otherwise the retraction window could be pushed out indefinitely."""
    _, headers = await _owner(api.client)
    await api.client.delete(f"{PREFIX}/organizations/current", headers=headers)

    response = await api.client.delete(f"{PREFIX}/organizations/current", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "DELETION_ALREADY_REQUESTED"


async def test_the_owner_can_change_their_mind(api: Harness) -> None:
    _, headers = await _owner(api.client)
    await api.client.delete(f"{PREFIX}/organizations/current", headers=headers)

    response = await api.client.post(
        f"{PREFIX}/organizations/current/deletion/cancel", headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["deletion_requested_at"] is None


async def test_cancelling_without_a_pending_deletion_is_refused(api: Harness) -> None:
    _, headers = await _owner(api.client)

    response = await api.client.post(
        f"{PREFIX}/organizations/current/deletion/cancel", headers=headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NO_DELETION_PENDING"


# --------------------------------------------------------------------------
# The purge itself
# --------------------------------------------------------------------------


async def test_the_purge_leaves_nothing_behind(api: Harness) -> None:
    """EF-06's acceptance criterion, stated as a count on every tenant table."""
    _, headers = await _owner(api.client)
    await _member(api, headers)
    organization_id = await _organization_id(api, headers)
    await api.client.delete(f"{PREFIX}/organizations/current", headers=headers)

    before = await _rows_left(api, organization_id)
    assert before["organizations"] == 1
    assert before["users"] == 2
    assert before["audit_log"] > 0

    await _backdate_request(api, organization_id, days=DELETION_RETRACTION.days + 1)
    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        purged = await purge_due_organizations(session)

    assert uuid.UUID(organization_id) in purged
    after = await _rows_left(api, organization_id)
    assert after == dict.fromkeys(after, 0), f"rows survived the purge: {after}"


async def test_the_purge_spares_an_organization_still_in_its_window(api: Harness) -> None:
    """Six days in, the customer can still change their mind."""
    _, headers = await _owner(api.client)
    organization_id = await _organization_id(api, headers)
    await api.client.delete(f"{PREFIX}/organizations/current", headers=headers)
    await _backdate_request(api, organization_id, days=DELETION_RETRACTION.days - 1)

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        purged = await purge_due_organizations(session)

    assert uuid.UUID(organization_id) not in purged
    assert (await _rows_left(api, organization_id))["organizations"] == 1


async def test_the_purge_ignores_organizations_that_asked_for_nothing(api: Harness) -> None:
    _, headers = await _owner(api.client)
    organization_id = await _organization_id(api, headers)

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        purged = await purge_due_organizations(session)

    assert uuid.UUID(organization_id) not in purged


async def test_a_retracted_deletion_is_never_purged(api: Harness) -> None:
    """The whole point of the seven days: cancelling has to actually stop it."""
    _, headers = await _owner(api.client)
    organization_id = await _organization_id(api, headers)
    await api.client.delete(f"{PREFIX}/organizations/current", headers=headers)
    await _backdate_request(api, organization_id, days=DELETION_RETRACTION.days + 1)
    await api.client.post(f"{PREFIX}/organizations/current/deletion/cancel", headers=headers)

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        purged = await purge_due_organizations(session)

    assert uuid.UUID(organization_id) not in purged
    assert (await _rows_left(api, organization_id))["organizations"] == 1


async def test_the_purge_does_not_touch_a_neighbouring_organization(api: Harness) -> None:
    """A cross-tenant job is exactly where isolation is easiest to get wrong."""
    _, doomed = await _owner(api.client)
    _, spared = await _owner(api.client)
    doomed_id = await _organization_id(api, doomed)
    spared_id = await _organization_id(api, spared)

    await api.client.delete(f"{PREFIX}/organizations/current", headers=doomed)
    await _backdate_request(api, doomed_id, days=DELETION_RETRACTION.days + 1)

    factory = create_session_factory(api.engine)
    async with factory() as session, session.begin():
        await purge_due_organizations(session)

    assert (await _rows_left(api, doomed_id))["organizations"] == 0
    assert (await _rows_left(api, spared_id))["organizations"] == 1
    assert (await _rows_left(api, spared_id))["users"] == 1
