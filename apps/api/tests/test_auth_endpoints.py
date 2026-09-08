"""The account endpoints over HTTP.

Complements `test_auth_service.py`, which covers the rules. What is checked
here is what only appears at the boundary: status codes, the Problem Details
shape, and the cross-tenant access attempts of T-07 made through the API rather
than through SQL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

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
from app.main import create_app
from app.ratelimit import InMemoryRateLimiter

APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)

PREFIX = "/api/v1"
PASSWORD = "un mot de passe long"

pytestmark = pytest.mark.asyncio


def _key_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired to the app, backed by the test database."""
    private, public = _key_pair()
    monkeypatch.setenv("JWT_PRIVATE_KEY", private)
    monkeypatch.setenv("JWT_PUBLIC_KEY", public)
    monkeypatch.setenv("DATABASE_URL", APP_DATABASE_URL)
    get_settings.cache_clear()

    engine = create_async_engine(APP_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"no test database reachable ({type(exc).__name__})")

    # A limiter of its own per test: sharing one Redis would make each test
    # depend on how many requests the previous ones made.
    app = create_app(limiter=InMemoryRateLimiter())
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http

    await engine.dispose()
    get_settings.cache_clear()


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@test.cm"


async def _register(client: AsyncClient, email: str | None = None) -> dict[str, str]:
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Test User",
            "organization_name": "Test Org",
            "password": PASSWORD,
            "email": email or _email(),
        },
    )
    assert response.status_code == 201, response.text
    tokens: dict[str, str] = response.json()
    return tokens


# --------------------------------------------------------------------------
# Registration and sign-in
# --------------------------------------------------------------------------


async def test_registering_returns_a_token_pair(client: AsyncClient) -> None:
    tokens = await _register(client)

    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["expires_in"] == 15 * 60


async def test_a_short_password_is_refused(client: AsyncClient) -> None:
    """EF-01 asks for at least ten characters."""
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Test",
            "organization_name": "Org",
            "password": "court",
            "email": _email(),
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_registering_without_an_identifier_is_refused(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={"full_name": "Test", "organization_name": "Org", "password": PASSWORD},
    )

    assert response.status_code == 422


async def test_a_badly_formed_phone_is_refused(client: AsyncClient) -> None:
    """Numbers are stored in E.164 so Mobile Money can use them later."""
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Test",
            "organization_name": "Org",
            "password": PASSWORD,
            "phone": "0690000000",
        },
    )

    assert response.status_code == 422


async def test_an_unknown_field_is_refused_rather_than_ignored(client: AsyncClient) -> None:
    """A typo must not be silently dropped while the caller thinks it applied."""
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Test",
            "organization_name": "Org",
            "password": PASSWORD,
            "email": _email(),
            "rolle": "OWNER",
        },
    )

    assert response.status_code == 422


async def test_registering_twice_conflicts(client: AsyncClient) -> None:
    email = _email()
    await _register(client, email)

    response = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Test",
            "organization_name": "Org",
            "password": PASSWORD,
            "email": email,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ACCOUNT_EXISTS"


async def test_signing_in_returns_a_token_pair(client: AsyncClient) -> None:
    email = _email()
    await _register(client, email)

    response = await client.post(
        f"{PREFIX}/auth/token", json={"email": email, "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_a_wrong_password_is_unauthorised(client: AsyncClient) -> None:
    email = _email()
    await _register(client, email)

    response = await client.post(
        f"{PREFIX}/auth/token", json={"email": email, "password": "le mauvais mot"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"


async def test_an_unknown_account_is_indistinguishable_from_a_wrong_password(
    client: AsyncClient,
) -> None:
    """Otherwise sign-in becomes a way to enumerate customers."""
    response = await client.post(
        f"{PREFIX}/auth/token", json={"email": _email(), "password": PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


async def test_me_returns_the_caller_and_the_organization(client: AsyncClient) -> None:
    email = _email()
    tokens = await _register(client, email)

    response = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == email
    assert body["user"]["role"] == "OWNER"
    assert body["organization"]["name"] == "Test Org"


async def test_me_without_a_token_is_refused(client: AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/me")

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "UNAUTHENTICATED"
    # Even a refusal carries the identifier support will ask for.
    assert body["debug_id"].startswith("DBG-")


async def test_me_with_a_forged_token_is_refused(client: AsyncClient) -> None:
    response = await client.get(
        f"{PREFIX}/me", headers={"Authorization": "Bearer not.a.real.token"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


async def test_refreshing_returns_a_new_pair(client: AsyncClient) -> None:
    tokens = await _register(client)

    response = await client.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 200
    assert response.json()["refresh_token"] != tokens["refresh_token"]


async def test_replaying_a_refresh_token_kills_the_session(client: AsyncClient) -> None:
    """Section 17.3 reuse detection, seen from the outside."""
    tokens = await _register(client)

    first = await client.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]

    replay = await client.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "REFRESH_TOKEN_REUSED"

    # The replacement is dead too, which is the point of revoking the family.
    after = await client.post(f"{PREFIX}/auth/refresh", json={"refresh_token": rotated})
    assert after.status_code == 401


async def test_logging_out_revokes_the_session(client: AsyncClient) -> None:
    tokens = await _register(client)

    logout = await client.post(
        f"{PREFIX}/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert logout.status_code == 204

    response = await client.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


async def test_logging_out_with_an_unknown_token_still_answers_204(
    client: AsyncClient,
) -> None:
    """A different answer would reveal whether a token is still live."""
    response = await client.post(f"{PREFIX}/auth/logout", json={"refresh_token": "never-existed"})

    assert response.status_code == 204


# --------------------------------------------------------------------------
# T-07 through the API: the IDOR half of the test
# --------------------------------------------------------------------------


async def test_a_token_only_ever_shows_its_own_organization(client: AsyncClient) -> None:
    """Two tenants, two tokens, no crossing over."""
    first = await _register(client)
    second = await _register(client)

    first_me = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {first['access_token']}"}
    )
    second_me = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {second['access_token']}"}
    )

    first_org = first_me.json()["organization"]["id"]
    second_org = second_me.json()["organization"]["id"]
    assert first_org != second_org
    assert first_me.json()["user"]["id"] != second_me.json()["user"]["id"]


async def test_a_revoked_member_is_refused_immediately(client: AsyncClient) -> None:
    """EF-03: revocation does not wait for the access token to expire."""
    tokens = await _register(client)
    me = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    user_id = me.json()["user"]["id"]
    organization_id = me.json()["organization"]["id"]

    engine = create_async_engine(APP_DATABASE_URL, poolclass=NullPool)
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": organization_id},
        )
        await session.execute(
            text("UPDATE users SET revoked_at = now() WHERE id = :id"), {"id": user_id}
        )
    await engine.dispose()

    # The access token is still cryptographically valid; the account is not.
    response = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


async def test_errors_use_the_problem_details_content_type(client: AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/me")

    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_rejected_password_is_never_echoed_back(client: AsyncClient) -> None:
    """Pydantic reports the rejected value; we must not forward it.

    A 422 body travels through proxies and access logs. Putting the submitted
    password in it would be us creating the leak, not the framework.
    """
    secret = "court"
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "full_name": "Test",
            "organization_name": "Org",
            "password": secret,
            "email": _email(),
        },
    )

    assert response.status_code == 422
    assert secret not in response.text
    # The caller still learns which field was wrong and why.
    fields = [error["field"] for error in response.json()["errors"]]
    assert any("password" in field for field in fields)


async def test_the_auth_endpoints_are_rate_limited(client: AsyncClient) -> None:
    """Section 17.3: 10 a minute on sign-in.

    This is where an attacker guesses passwords and probes which addresses are
    customers. The strict allowance is the point of having two.
    """
    email = _email()
    payload = {"email": email, "password": "le mauvais mot de passe"}

    statuses = [
        (await client.post(f"{PREFIX}/auth/token", json=payload)).status_code for _ in range(12)
    ]

    assert 429 in statuses, f"expected a refusal within 12 attempts, saw {set(statuses)}"
    first_refusal = statuses.index(429)
    assert first_refusal >= 10, f"refused after {first_refusal}, expected at least 10"


async def test_a_refused_request_says_when_to_retry(client: AsyncClient) -> None:
    payload = {"email": _email(), "password": "le mauvais mot de passe"}
    response = None
    for _ in range(12):
        response = await client.post(f"{PREFIX}/auth/token", json=payload)
        if response.status_code == 429:
            break

    assert response is not None
    assert response.status_code == 429
    assert response.json()["code"] == "RATE_LIMITED"
    assert int(response.headers["Retry-After"]) > 0
    # Still Problem Details, still carrying the identifier for support.
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["debug_id"].startswith("DBG-")


async def test_health_is_never_rate_limited(client: AsyncClient) -> None:
    """Throttling a probe turns a busy moment into a restart loop."""
    statuses = {(await client.get("/health")).status_code for _ in range(30)}

    assert statuses == {200}
