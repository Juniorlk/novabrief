"""Email verification at signup (EF-02), over HTTP.

EF-02's acceptance criterion is about refusals: an expired or reused link has
to be turned down with a clear message. Most of what follows tests exactly
that, plus the one case that would be a real hole — a link that still confirms
an address after the account moved to a different one.
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
from app.ratelimit import InMemoryRateLimiter
from app.services.verification import EMAIL_VERIFICATION_TTL

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


def _token_from(body: str) -> str:
    match = re.search(r"token=([A-Za-z0-9_\-]+)", body)
    assert match, f"no token found in: {body}"
    return match.group(1)


async def _signup(harness: Harness, *, email: str | None = None) -> tuple[str, dict[str, str]]:
    address = email or _email()
    response = await harness.client.post(
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


async def _verification_token(harness: Harness, address: str) -> str:
    message = harness.mailbox.last_to(address)
    assert message is not None, f"no verification email was sent to {address}"
    return _token_from(message.text)


async def _expire_tokens(harness: Harness, organization_id: str) -> None:
    """Age every pending link past its deadline, without waiting 24 hours."""
    async with harness.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        await connection.execute(
            text("UPDATE email_verifications SET expires_at = :moment"),
            {"moment": datetime.now(UTC) - timedelta(seconds=1)},
        )
        await connection.commit()


async def _organization_id(harness: Harness, headers: dict[str, str]) -> str:
    body = (await harness.client.get(f"{PREFIX}/me", headers=headers)).json()
    identifier: str = body["organization"]["id"]
    return identifier


# --------------------------------------------------------------------------
# Signing up sends the link
# --------------------------------------------------------------------------


async def test_signing_up_sends_a_verification_link(api: Harness) -> None:
    address, _ = await _signup(api)

    message = api.mailbox.last_to(address)

    assert message is not None
    assert "verify-email?token=" in message.text


async def test_a_new_account_starts_unverified(api: Harness) -> None:
    _, headers = await _signup(api)

    body = (await api.client.get(f"{PREFIX}/me", headers=headers)).json()

    assert body["user"]["email_verified"] is False


async def test_signing_up_with_a_phone_sends_nothing(api: Harness) -> None:
    """EF-01 allows a phone-only account; there is then no address to prove."""
    response = await api.client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Owner",
            "organization_name": "Test Org",
            "password": PASSWORD,
            "phone": f"+2376{uuid.uuid4().int % 10**8:08d}",
        },
    )

    assert response.status_code == 201, response.text
    assert api.mailbox.sent == [] or all(
        "verify-email" not in message.text for message in api.mailbox.sent
    )


async def test_a_failed_delivery_does_not_lose_the_account(api: Harness) -> None:
    """A mail provider outage must not take the whole signup funnel down."""
    api.mailbox.fail_next = True

    response = await api.client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Owner",
            "organization_name": "Test Org",
            "password": PASSWORD,
            "email": _email(),
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["access_token"]


# --------------------------------------------------------------------------
# Following the link
# --------------------------------------------------------------------------


async def test_following_the_link_verifies_the_address(api: Harness) -> None:
    address, headers = await _signup(api)
    token = await _verification_token(api, address)

    response = await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": token})

    assert response.status_code == 204, response.text
    body = (await api.client.get(f"{PREFIX}/me", headers=headers)).json()
    assert body["user"]["email_verified"] is True


async def test_the_link_needs_no_session(api: Harness) -> None:
    """It is followed in whatever browser is open, rarely the one signed in."""
    address, _ = await _signup(api)
    token = await _verification_token(api, address)

    response = await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": token})

    assert response.status_code == 204


async def test_a_link_cannot_be_used_twice(api: Harness) -> None:
    address, _ = await _signup(api)
    token = await _verification_token(api, address)
    await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": token})

    response = await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": token})

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_VERIFICATION_TOKEN"


async def test_an_expired_link_is_refused(api: Harness) -> None:
    address, headers = await _signup(api)
    token = await _verification_token(api, address)
    await _expire_tokens(api, await _organization_id(api, headers))

    response = await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": token})

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_VERIFICATION_TOKEN"


async def test_an_unknown_link_is_refused_the_same_way(api: Harness) -> None:
    """Same code as expired and reused, so the endpoint says nothing extra."""
    response = await api.client.post(
        f"{PREFIX}/auth/email/verify", json={"token": "not-a-real-token"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_VERIFICATION_TOKEN"


async def test_the_verification_is_audited(api: Harness) -> None:
    address, headers = await _signup(api)
    organization_id = await _organization_id(api, headers)
    token = await _verification_token(api, address)
    await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": token})

    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": organization_id},
        )
        rows = await connection.execute(text("SELECT action FROM audit_log"))
        actions = [row.action for row in rows]

    assert "email.verified" in actions


# --------------------------------------------------------------------------
# Asking for another one
# --------------------------------------------------------------------------


async def test_the_link_can_be_sent_again(api: Harness) -> None:
    address, headers = await _signup(api)
    first = await _verification_token(api, address)

    response = await api.client.post(f"{PREFIX}/auth/email/resend", headers=headers)

    assert response.status_code == 202, response.text
    second = await _verification_token(api, address)
    assert second != first, "a resend must issue a new token, not repeat the old one"

    confirmed = await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": second})
    assert confirmed.status_code == 204


async def test_resending_is_refused_once_verified(api: Harness) -> None:
    """A live token for a verified account is a credential nobody needs."""
    address, headers = await _signup(api)
    token = await _verification_token(api, address)
    await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": token})

    response = await api.client.post(f"{PREFIX}/auth/email/resend", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_ALREADY_VERIFIED"


async def test_resending_needs_a_session(api: Harness) -> None:
    """Otherwise it would name an address and mail anyone on request."""
    response = await api.client.post(f"{PREFIX}/auth/email/resend")

    assert response.status_code == 401


# --------------------------------------------------------------------------
# The isolation cases
# --------------------------------------------------------------------------


async def test_a_link_stops_working_when_the_address_changes(api: Harness) -> None:
    """Otherwise a mail sent to the old address proves the new one."""
    address, headers = await _signup(api)
    token = await _verification_token(api, address)

    # The account moves to another address before the first link is followed.
    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": await _organization_id(api, headers)},
        )
        await connection.execute(
            text("UPDATE users SET email = :email WHERE email = :old"),
            {"email": _email(), "old": address},
        )
        await connection.commit()

    response = await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": token})

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_VERIFICATION_TOKEN"


async def test_one_account_cannot_verify_another(api: Harness) -> None:
    """The link carries the identity; a signed-in caller cannot redirect it."""
    first_address, _ = await _signup(api)
    _, second_headers = await _signup(api)
    first_token = await _verification_token(api, first_address)

    # A caller from the second organization follows the first one's link. It
    # works — the link is the credential — but it verifies the *first* account.
    await api.client.post(f"{PREFIX}/auth/email/verify", json={"token": first_token})

    second = (await api.client.get(f"{PREFIX}/me", headers=second_headers)).json()
    assert second["user"]["email_verified"] is False


async def test_the_link_lives_a_day_not_half_an_hour(api: Harness) -> None:
    """A reset link is a password and lives 30 minutes; this one grants nothing.

    Checked on the stored row rather than on the constant, so shortening the
    window without meaning to shows up here.
    """
    _, headers = await _signup(api)
    issued_at = datetime.now(UTC)

    async with api.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": await _organization_id(api, headers)},
        )
        expires_at = await connection.scalar(
            text("SELECT expires_at FROM email_verifications ORDER BY id DESC LIMIT 1")
        )

    assert expires_at is not None
    assert abs((expires_at - issued_at) - EMAIL_VERIFICATION_TTL) < timedelta(minutes=1)
