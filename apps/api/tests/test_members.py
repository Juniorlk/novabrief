"""Invitations and password reset (EF-02, EF-03), over HTTP.

Both features hand out a link that grants access, so what is tested is mostly
what happens when the link is old, used twice, or aimed at the wrong tenant.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
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


@pytest_asyncio.fixture
async def setup(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, RecordingProvider]]:
    """A client whose emails are captured instead of sent."""
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
        yield http, mailbox

    await engine.dispose()
    get_settings.cache_clear()


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@test.cm"


def _token_from(body: str) -> str:
    """Pull the token out of a link in an email body."""
    match = re.search(r"token=([A-Za-z0-9_\-]+)", body)
    assert match, f"no token found in: {body}"
    return match.group(1)


async def _owner(client: AsyncClient) -> tuple[str, dict[str, str]]:
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
    tokens: dict[str, str] = response.json()
    return address, {"Authorization": f"Bearer {tokens['access_token']}"}


# --------------------------------------------------------------------------
# Invitations (EF-03)
# --------------------------------------------------------------------------


async def test_an_admin_can_invite_a_member(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    client, mailbox = setup
    _, headers = await _owner(client)
    invitee = _email()

    response = await client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": invitee, "role": "MEMBER"},
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["email"] == invitee
    assert mailbox.last_to(invitee) is not None


async def test_the_invitation_token_is_never_returned_to_the_inviter(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """The token grants account creation; only the invitee should hold it."""
    client, mailbox = setup
    _, headers = await _owner(client)
    invitee = _email()

    response = await client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": invitee},
        headers=headers,
    )

    message = mailbox.last_to(invitee)
    assert message is not None
    token = _token_from(message.text)
    assert token not in response.text


async def test_inviting_requires_authentication(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    client, _ = setup

    response = await client.post(
        f"{PREFIX}/organizations/current/members", json={"email": _email()}
    )

    assert response.status_code == 401


async def test_a_plain_member_cannot_invite(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """Only Admin and Owner administer the organization."""
    client, mailbox = setup
    _, headers = await _owner(client)
    invitee = _email()
    await client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": invitee, "role": "MEMBER"},
        headers=headers,
    )
    message = mailbox.last_to(invitee)
    assert message is not None

    await client.post(
        f"{PREFIX}/auth/invitations/accept",
        json={
            "token": _token_from(message.text),
            "full_name": "Member",
            "password": PASSWORD,
        },
    )
    signed_in = await client.post(
        f"{PREFIX}/auth/token", json={"email": invitee, "password": PASSWORD}
    )
    member_headers = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}

    response = await client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": _email()},
        headers=member_headers,
    )

    assert response.status_code == 403


async def test_accepting_an_invitation_creates_the_account(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    client, mailbox = setup
    _, headers = await _owner(client)
    invitee = _email()
    await client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": invitee, "role": "ADMIN"},
        headers=headers,
    )
    message = mailbox.last_to(invitee)
    assert message is not None

    accepted = await client.post(
        f"{PREFIX}/auth/invitations/accept",
        json={
            "token": _token_from(message.text),
            "full_name": "New Member",
            "password": PASSWORD,
        },
    )
    assert accepted.status_code == 204

    signed_in = await client.post(
        f"{PREFIX}/auth/token", json={"email": invitee, "password": PASSWORD}
    )
    assert signed_in.status_code == 200
    me = await client.get(
        f"{PREFIX}/me",
        headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"},
    )
    assert me.json()["user"]["role"] == "ADMIN"
    # Following the emailed link proves the address as well as a separate
    # verification message would.
    assert me.json()["user"]["email_verified"] is True


async def test_an_invitation_cannot_be_accepted_twice(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    client, mailbox = setup
    _, headers = await _owner(client)
    invitee = _email()
    await client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": invitee},
        headers=headers,
    )
    message = mailbox.last_to(invitee)
    assert message is not None
    token = _token_from(message.text)

    first = await client.post(
        f"{PREFIX}/auth/invitations/accept",
        json={"token": token, "full_name": "A", "password": PASSWORD},
    )
    assert first.status_code == 204

    second = await client.post(
        f"{PREFIX}/auth/invitations/accept",
        json={"token": token, "full_name": "B", "password": PASSWORD},
    )
    assert second.status_code == 400
    assert second.json()["code"] == "INVALID_INVITATION"


async def test_an_unknown_invitation_token_is_refused(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    client, _ = setup

    response = await client.post(
        f"{PREFIX}/auth/invitations/accept",
        json={"token": "never-existed", "full_name": "X", "password": PASSWORD},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INVITATION"


async def test_inviting_an_existing_account_is_refused(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """A person belongs to one organization in V1."""
    client, _ = setup
    existing, _headers = await _owner(client)
    _, headers = await _owner(client)

    response = await client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": existing},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ACCOUNT_EXISTS"


async def test_members_are_listed_per_organization(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """RLS scopes the list; no filter is written in the endpoint."""
    client, _ = setup
    first_email, first_headers = await _owner(client)
    _, second_headers = await _owner(client)

    first = await client.get(f"{PREFIX}/organizations/current/members", headers=first_headers)
    second = await client.get(f"{PREFIX}/organizations/current/members", headers=second_headers)

    first_addresses = {m["email"] for m in first.json()}
    second_addresses = {m["email"] for m in second.json()}
    assert first_email in first_addresses
    assert first_email not in second_addresses


async def test_revoking_a_member_ends_their_session(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """EF-03: access ends in under a minute."""
    client, mailbox = setup
    _, headers = await _owner(client)
    invitee = _email()
    await client.post(
        f"{PREFIX}/organizations/current/members",
        json={"email": invitee},
        headers=headers,
    )
    message = mailbox.last_to(invitee)
    assert message is not None
    await client.post(
        f"{PREFIX}/auth/invitations/accept",
        json={"token": _token_from(message.text), "full_name": "M", "password": PASSWORD},
    )
    signed_in = await client.post(
        f"{PREFIX}/auth/token", json={"email": invitee, "password": PASSWORD}
    )
    member_tokens = signed_in.json()
    member_headers = {"Authorization": f"Bearer {member_tokens['access_token']}"}

    listing = await client.get(f"{PREFIX}/organizations/current/members", headers=headers)
    member_id = next(m["id"] for m in listing.json() if m["email"] == invitee)

    revoked = await client.delete(
        f"{PREFIX}/organizations/current/members/{member_id}", headers=headers
    )
    assert revoked.status_code == 204

    # The access token is still cryptographically valid; the account is not.
    assert (await client.get(f"{PREFIX}/me", headers=member_headers)).status_code == 401
    # And the refresh token is dead, so they cannot mint a new one.
    refreshed = await client.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": member_tokens["refresh_token"]}
    )
    assert refreshed.status_code == 401


async def test_the_owner_cannot_be_revoked(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """Otherwise an admin could lock the organization out of its own account."""
    client, _ = setup
    owner_email, headers = await _owner(client)
    listing = await client.get(f"{PREFIX}/organizations/current/members", headers=headers)
    owner_id = next(m["id"] for m in listing.json() if m["email"] == owner_email)

    response = await client.delete(
        f"{PREFIX}/organizations/current/members/{owner_id}", headers=headers
    )

    assert response.status_code == 409
    assert response.json()["code"] in {"CANNOT_REVOKE_SELF", "CANNOT_REVOKE_OWNER"}


async def test_revoking_a_member_of_another_organization_finds_nothing(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """T-07 through the API: an IDOR attempt on a member identifier."""
    client, _ = setup
    victim_email, victim_headers = await _owner(client)
    _, attacker_headers = await _owner(client)

    listing = await client.get(f"{PREFIX}/organizations/current/members", headers=victim_headers)
    victim_id = next(m["id"] for m in listing.json() if m["email"] == victim_email)

    response = await client.delete(
        f"{PREFIX}/organizations/current/members/{victim_id}", headers=attacker_headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == "MEMBER_NOT_FOUND"


# --------------------------------------------------------------------------
# Password reset (EF-02)
# --------------------------------------------------------------------------


async def test_a_reset_link_is_emailed(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    client, mailbox = setup
    address, _ = await _owner(client)

    response = await client.post(f"{PREFIX}/auth/password/reset", json={"email": address})

    assert response.status_code == 202
    assert mailbox.last_to(address) is not None


async def test_an_unknown_address_gets_the_same_answer(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """This endpoint needs no credential, so it must not confirm an address."""
    client, mailbox = setup
    unknown = _email()

    response = await client.post(f"{PREFIX}/auth/password/reset", json={"email": unknown})

    assert response.status_code == 202
    assert mailbox.last_to(unknown) is None, "nothing should be sent to an unknown address"


async def test_the_reset_link_changes_the_password(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    client, mailbox = setup
    address, _ = await _owner(client)
    await client.post(f"{PREFIX}/auth/password/reset", json={"email": address})
    message = mailbox.last_to(address)
    assert message is not None

    new_password = "un tout autre mot de passe"
    confirmed = await client.post(
        f"{PREFIX}/auth/password/confirm",
        json={"token": _token_from(message.text), "password": new_password},
    )
    assert confirmed.status_code == 204

    assert (
        await client.post(f"{PREFIX}/auth/token", json={"email": address, "password": PASSWORD})
    ).status_code == 401
    assert (
        await client.post(f"{PREFIX}/auth/token", json={"email": address, "password": new_password})
    ).status_code == 200


async def test_a_reset_link_works_only_once(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """EF-02: a reused link is refused with a clear message."""
    client, mailbox = setup
    address, _ = await _owner(client)
    await client.post(f"{PREFIX}/auth/password/reset", json={"email": address})
    message = mailbox.last_to(address)
    assert message is not None
    token = _token_from(message.text)

    first = await client.post(
        f"{PREFIX}/auth/password/confirm", json={"token": token, "password": "premier choix ok"}
    )
    assert first.status_code == 204

    second = await client.post(
        f"{PREFIX}/auth/password/confirm", json={"token": token, "password": "second choix ok"}
    )
    assert second.status_code == 400
    assert second.json()["code"] == "INVALID_RESET_TOKEN"


async def test_an_expired_reset_link_is_refused(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """EF-02 fixes the window at 30 minutes."""
    client, mailbox = setup
    address, headers = await _owner(client)
    await client.post(f"{PREFIX}/auth/password/reset", json={"email": address})
    message = mailbox.last_to(address)
    assert message is not None

    me = await client.get(f"{PREFIX}/me", headers=headers)
    organization_id = me.json()["organization"]["id"]

    engine = create_async_engine(APP_DATABASE_URL, poolclass=NullPool)
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": organization_id},
        )
        await session.execute(
            text("UPDATE password_resets SET expires_at = :past"),
            {"past": datetime.now(UTC) - timedelta(minutes=1)},
        )
    await engine.dispose()

    response = await client.post(
        f"{PREFIX}/auth/password/confirm",
        json={"token": _token_from(message.text), "password": "un nouveau mot de passe"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_RESET_TOKEN"


async def test_resetting_a_password_revokes_every_session(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    """Someone resetting often believes their account is compromised.

    Leaving the attacker's sessions alive would defeat the exercise.
    """
    client, mailbox = setup
    address, _ = await _owner(client)
    signed_in = await client.post(
        f"{PREFIX}/auth/token", json={"email": address, "password": PASSWORD}
    )
    old_refresh = signed_in.json()["refresh_token"]

    await client.post(f"{PREFIX}/auth/password/reset", json={"email": address})
    message = mailbox.last_to(address)
    assert message is not None
    await client.post(
        f"{PREFIX}/auth/password/confirm",
        json={"token": _token_from(message.text), "password": "un nouveau mot de passe"},
    )

    response = await client.post(f"{PREFIX}/auth/refresh", json={"refresh_token": old_refresh})
    assert response.status_code == 401


async def test_a_short_new_password_is_refused(
    setup: tuple[AsyncClient, RecordingProvider],
) -> None:
    client, _ = setup

    response = await client.post(
        f"{PREFIX}/auth/password/confirm", json={"token": "whatever", "password": "court"}
    )

    assert response.status_code == 422
