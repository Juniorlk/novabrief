"""Profile and organization settings (EF-04, EF-05), over HTTP.

What is worth testing here is not that a name can be changed. It is what the
endpoints refuse: a member editing the organization, anyone promoting
themselves, a retention raised beyond what the plan paid for, and a timezone
that would only fail months later inside a scheduled job.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

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
from app.ratelimit import InMemoryRateLimiter

APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)

PREFIX = "/api/v1"
PASSWORD = "un mot de passe long"

pytestmark = pytest.mark.asyncio


@dataclass
class Harness:
    """Everything a test here needs: the API, the mailbox and the database."""

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

    # NullPool on purpose: the default pool keeps connections alive past the
    # end of the test, which surfaces later as an unraisable exception in
    # whichever test happens to run next.
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
    """Register an organization and return its Owner's auth header."""
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


async def _member(harness: Harness, owner: dict[str, str]) -> dict[str, str]:
    """Invite, accept and sign in an ordinary MEMBER of the same organization."""
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
        json={
            "token": match.group(1),
            "full_name": "Ordinary Member",
            "password": PASSWORD,
        },
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


async def _audit_actions(harness: Harness, organization_id: str) -> list[str]:
    """The audit trail of one organization, oldest first."""
    async with harness.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        rows = await connection.execute(text("SELECT action FROM audit_log ORDER BY id"))
        return [row.action for row in rows]


# --------------------------------------------------------------------------
# Profile (EF-04)
# --------------------------------------------------------------------------


async def test_a_user_updates_their_own_profile(api: Harness) -> None:
    headers = await _owner(api.client)

    response = await api.client.patch(
        f"{PREFIX}/me",
        json={"full_name": "Junior Lekeu", "locale": "en", "timezone": "Europe/Paris"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    user = response.json()["user"]
    assert user["full_name"] == "Junior Lekeu"
    assert user["locale"] == "en"
    assert user["timezone"] == "Europe/Paris"

    # And it survives the request that wrote it.
    again = await api.client.get(f"{PREFIX}/me", headers=headers)
    assert again.json()["user"]["full_name"] == "Junior Lekeu"


async def test_an_omitted_field_is_left_alone(api: Harness) -> None:
    """A partial update must not reset what the client did not send."""
    headers = await _owner(api.client)
    await api.client.patch(f"{PREFIX}/me", json={"locale": "en"}, headers=headers)

    response = await api.client.patch(
        f"{PREFIX}/me", json={"full_name": "Renamed"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["user"]["locale"] == "en"


async def test_an_empty_body_changes_nothing(api: Harness) -> None:
    headers = await _owner(api.client)
    before = (await api.client.get(f"{PREFIX}/me", headers=headers)).json()

    response = await api.client.patch(f"{PREFIX}/me", json={}, headers=headers)

    assert response.status_code == 200
    assert response.json()["user"] == before["user"]


async def test_a_caller_cannot_promote_themselves(api: Harness) -> None:
    """The refusal that matters most: role is not an editable field."""
    headers = await _owner(api.client)
    member = await _member(api, headers)

    response = await api.client.patch(f"{PREFIX}/me", json={"role": "OWNER"}, headers=member)

    assert response.status_code == 422
    after = (await api.client.get(f"{PREFIX}/me", headers=member)).json()
    assert after["user"]["role"] == "MEMBER"


async def test_an_unknown_timezone_is_refused(api: Harness) -> None:
    headers = await _owner(api.client)

    response = await api.client.patch(
        f"{PREFIX}/me", json={"timezone": "Mars/Olympus"}, headers=headers
    )

    assert response.status_code == 422


async def test_a_null_is_refused_where_the_column_is_not_nullable(api: Harness) -> None:
    headers = await _owner(api.client)

    response = await api.client.patch(f"{PREFIX}/me", json={"full_name": None}, headers=headers)

    assert response.status_code == 422


async def test_updating_a_profile_needs_a_token(api: Harness) -> None:
    response = await api.client.patch(f"{PREFIX}/me", json={"full_name": "Anonymous"})

    assert response.status_code == 401


# --------------------------------------------------------------------------
# Organization (EF-05)
# --------------------------------------------------------------------------


async def test_an_owner_updates_the_organization(api: Harness) -> None:
    headers = await _owner(api.client)

    response = await api.client.patch(
        f"{PREFIX}/organizations/current",
        json={
            "name": "Novafrik SARL",
            "legal_id": "RC/DLA/2019/B/1234",
            "default_language": "en",
            "lexicon": ["Novafrik", "NovaBrief", "RCCM"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Novafrik SARL"
    assert body["legal_id"] == "RC/DLA/2019/B/1234"
    assert body["default_language"] == "en"
    assert body["lexicon"] == ["Novafrik", "NovaBrief", "RCCM"]


async def test_an_ordinary_member_cannot_update_the_organization(api: Harness) -> None:
    """EF-05 is an administrator's screen; a member may read it, never write it."""
    owner = await _owner(api.client)
    member = await _member(api, owner)

    response = await api.client.patch(
        f"{PREFIX}/organizations/current", json={"name": "Hijacked"}, headers=member
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


async def test_audio_retention_can_be_reduced(api: Harness) -> None:
    headers = await _owner(api.client)

    response = await api.client.patch(
        f"{PREFIX}/organizations/current", json={"audio_retention_days": 7}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["audio_retention_days"] == 7


async def test_audio_retention_cannot_be_raised(api: Harness) -> None:
    """Keeping audio longer is what the plan pays for (ADR-06, ADR-09)."""
    headers = await _owner(api.client)

    response = await api.client.patch(
        f"{PREFIX}/organizations/current", json={"audio_retention_days": 365}, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == "RETENTION_INCREASE_REQUIRES_PLAN"


async def test_billing_state_is_not_editable(api: Harness) -> None:
    """A customer must not be able to grant themselves a plan or a quota."""
    headers = await _owner(api.client)

    for forbidden in ({"plan_code": "business"}, {"quota_seconds": 10**9}, {"status": "ACTIVE"}):
        response = await api.client.patch(
            f"{PREFIX}/organizations/current", json=forbidden, headers=headers
        )
        assert response.status_code == 422, f"{forbidden} was accepted"


async def test_the_lexicon_is_trimmed_and_deduplicated(api: Harness) -> None:
    """It is sent to the transcription provider, which charges for the list."""
    headers = await _owner(api.client)

    response = await api.client.patch(
        f"{PREFIX}/organizations/current",
        json={"lexicon": ["  Novafrik  ", "novafrik", " ", "Douala"]},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["lexicon"] == ["Novafrik", "Douala"]


async def test_the_legal_identifier_can_be_cleared(api: Harness) -> None:
    """The one field a null may empty, because it is nullable in the database."""
    headers = await _owner(api.client)
    await api.client.patch(
        f"{PREFIX}/organizations/current", json={"legal_id": "RC/DLA/2019/B/1234"}, headers=headers
    )

    response = await api.client.patch(
        f"{PREFIX}/organizations/current", json={"legal_id": None}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["legal_id"] is None


async def test_an_unknown_field_is_refused_rather_than_ignored(api: Harness) -> None:
    """A typo the client believes was applied is worse than a visible error."""
    headers = await _owner(api.client)

    response = await api.client.patch(
        f"{PREFIX}/organizations/current", json={"nmae": "typo"}, headers=headers
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Audit trail (section 21.2)
# --------------------------------------------------------------------------


async def test_both_updates_are_audited(api: Harness) -> None:
    headers = await _owner(api.client)
    organization_id = await _organization_id(api, headers)

    await api.client.patch(f"{PREFIX}/me", json={"locale": "en"}, headers=headers)
    await api.client.patch(
        f"{PREFIX}/organizations/current", json={"name": "Audited"}, headers=headers
    )

    actions = await _audit_actions(api, organization_id)
    assert "profile.updated" in actions
    assert "organization.updated" in actions


async def test_the_audit_entry_records_the_fields_not_their_values(api: Harness) -> None:
    """A full name is personal data; the audit log is read by operators."""
    headers = await _owner(api.client)
    organization_id = await _organization_id(api, headers)

    await api.client.patch(f"{PREFIX}/me", json={"full_name": "Secret Name"}, headers=headers)

    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        row = (
            await connection.execute(
                text(
                    "SELECT metadata::text AS payload FROM audit_log "
                    "WHERE action = 'profile.updated' ORDER BY id DESC LIMIT 1"
                )
            )
        ).one()

    assert "full_name" in row.payload
    assert "Secret Name" not in row.payload


async def test_a_no_op_update_writes_no_audit_entry(api: Harness) -> None:
    """An empty diff is not an event; auditing it would bury the real ones."""
    headers = await _owner(api.client)
    organization_id = await _organization_id(api, headers)

    await api.client.patch(f"{PREFIX}/me", json={}, headers=headers)

    assert "profile.updated" not in await _audit_actions(api, organization_id)
