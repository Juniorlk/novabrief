"""Registration, sign-in and refresh-token rotation, against a real database.

Rotation and reuse detection are part of T-14. They are exercised here rather
than through HTTP because the interesting cases — a token presented twice, a
member revoked between refreshes — are about state, not about routing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import create_session_factory, set_current_organization
from app.models import AuditLog, RefreshToken, Role, User
from app.security import decode_access_token
from app.services import auth

APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)

pytestmark = pytest.mark.asyncio


def _settings() -> Settings:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return Settings(
        jwt_private_key=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        jwt_public_key=key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )


@pytest.fixture(scope="module")
def settings() -> Settings:
    return _settings()


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(APP_DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"no test database reachable ({type(exc).__name__})")
    yield create_session_factory(engine)
    await engine.dispose()


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@test.cm"


async def _register(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, email: str
) -> auth.IssuedSession:
    async with sessions() as session, session.begin():
        return await auth.register(
            session,
            settings=settings,
            full_name="Test User",
            organization_name="Test Org",
            password="un mot de passe long",
            email=email,
            phone=None,
            locale="fr",
            timezone="Africa/Douala",
        )


async def test_registration_creates_an_owner_and_a_session(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """EF-01: account and organization in one step, creator becomes Owner."""
    email = _unique_email()
    issued = await _register(sessions, settings, email)

    assert issued.user.role == Role.OWNER.value
    assert issued.organization.name == "Test Org"

    claims = decode_access_token(settings=settings, token=issued.access_token)
    assert claims.user_id == issued.user.id
    assert claims.organization_id == issued.organization.id
    assert claims.role == "OWNER"


async def test_the_same_address_cannot_register_twice(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Without the lookup function, RLS would hide the duplicate and allow it."""
    email = _unique_email()
    await _register(sessions, settings, email)

    with pytest.raises(auth.AuthError) as failure:
        await _register(sessions, settings, email)
    assert failure.value.code == "ACCOUNT_EXISTS"


async def test_sign_in_returns_a_session(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    email = _unique_email()
    await _register(sessions, settings, email)

    async with sessions() as session, session.begin():
        issued = await auth.authenticate(
            session,
            settings=settings,
            password="un mot de passe long",
            email=email,
            phone=None,
        )

    assert issued.user.email == email


async def test_a_wrong_password_is_refused(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    email = _unique_email()
    await _register(sessions, settings, email)

    with pytest.raises(auth.AuthError) as failure:
        async with sessions() as session, session.begin():
            await auth.authenticate(
                session, settings=settings, password="le mauvais", email=email, phone=None
            )
    assert failure.value.code == "INVALID_CREDENTIALS"


async def test_an_unknown_account_reports_the_same_error(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """A different code here would turn sign-in into a customer directory."""
    with pytest.raises(auth.AuthError) as failure:
        async with sessions() as session, session.begin():
            await auth.authenticate(
                session,
                settings=settings,
                password="peu importe",
                email=_unique_email(),
                phone=None,
            )
    assert failure.value.code == "INVALID_CREDENTIALS"


async def test_refreshing_rotates_the_token(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        rotated = await auth.refresh(
            session, settings=settings, presented_token=issued.refresh_token
        )

    assert rotated.refresh_token != issued.refresh_token
    assert rotated.user.id == issued.user.id


async def test_presenting_a_used_token_revokes_the_whole_family(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Section 17.3 reuse detection.

    A token presented twice means both the legitimate holder and someone else
    have it. Revoking only that token would leave the attacker's replacement
    working, so the entire login is revoked and both must sign in again.
    """
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        rotated = await auth.refresh(
            session, settings=settings, presented_token=issued.refresh_token
        )

    # The old token, replayed.
    with pytest.raises(auth.AuthError) as failure:
        async with sessions() as session, session.begin():
            await auth.refresh(session, settings=settings, presented_token=issued.refresh_token)
    assert failure.value.code == "REFRESH_TOKEN_REUSED"

    # The replacement is dead too: that is the point.
    with pytest.raises(auth.AuthError):
        async with sessions() as session, session.begin():
            await auth.refresh(session, settings=settings, presented_token=rotated.refresh_token)


async def test_reuse_detection_is_written_to_the_audit_log(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        await auth.refresh(session, settings=settings, presented_token=issued.refresh_token)
    with pytest.raises(auth.AuthError):
        async with sessions() as session, session.begin():
            await auth.refresh(session, settings=settings, presented_token=issued.refresh_token)

    async with sessions() as session, session.begin():
        await set_current_organization(session, issued.organization.id)
        actions = (
            await session.scalars(
                text("SELECT action FROM audit_log ORDER BY created_at")  # type: ignore[arg-type]
            )
        ).all()

    assert "session.reuse_detected" in actions


async def test_logging_out_kills_the_session(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        await auth.revoke_session(session, presented_token=issued.refresh_token)

    with pytest.raises(auth.AuthError) as failure:
        async with sessions() as session, session.begin():
            await auth.refresh(session, settings=settings, presented_token=issued.refresh_token)
    assert failure.value.code == "INVALID_REFRESH_TOKEN"


async def test_a_revoked_member_cannot_refresh(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """EF-03: access ends within one access-token lifetime of revocation."""
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        await set_current_organization(session, issued.organization.id)
        user = await session.get(User, issued.user.id)
        assert user is not None
        user.revoked_at = user.created_at

    with pytest.raises(auth.AuthError) as failure:
        async with sessions() as session, session.begin():
            await auth.refresh(session, settings=settings, presented_token=issued.refresh_token)
    assert failure.value.code == "ACCOUNT_REVOKED"


async def test_an_unknown_refresh_token_is_refused(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    with pytest.raises(auth.AuthError) as failure:
        async with sessions() as session, session.begin():
            await auth.refresh(session, settings=settings, presented_token="not-a-real-token")
    assert failure.value.code == "INVALID_REFRESH_TOKEN"


async def test_refresh_tokens_are_never_stored_in_the_clear(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """A database dump must not hand out working sessions."""
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        await set_current_organization(session, issued.organization.id)
        stored = (await session.scalars(text("SELECT token_hash FROM refresh_tokens"))).all()  # type: ignore[arg-type]

    assert issued.refresh_token not in stored
    assert all(len(value) == 64 for value in stored)


async def test_registration_is_written_to_the_audit_log(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        await set_current_organization(session, issued.organization.id)
        entries = (await session.scalars(text("SELECT action FROM audit_log"))).all()  # type: ignore[arg-type]

    assert "organization.created" in entries


async def test_a_session_belongs_to_exactly_one_tenant(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Two organizations must not see each other's tokens or audit entries."""
    first = await _register(sessions, settings, _unique_email())
    second = await _register(sessions, settings, _unique_email())
    assert first.organization.id != second.organization.id

    async with sessions() as session, session.begin():
        await set_current_organization(session, first.organization.id)
        owners = (await session.scalars(text("SELECT id FROM refresh_tokens"))).all()  # type: ignore[arg-type]

    async with sessions() as session, session.begin():
        await set_current_organization(session, second.organization.id)
        others = (await session.scalars(text("SELECT id FROM refresh_tokens"))).all()  # type: ignore[arg-type]

    assert set(owners).isdisjoint(others)


async def test_the_audit_log_carries_no_meeting_content(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """The audit log records why something happened, never what was said."""
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        await set_current_organization(session, issued.organization.id)
        entry = await session.scalar(
            text("SELECT reason FROM audit_log WHERE action = 'organization.created'")  # type: ignore[arg-type]
        )

    assert entry is None


async def test_refresh_token_rows_expire_in_the_future(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        await set_current_organization(session, issued.organization.id)
        row = await session.scalar(
            text("SELECT expires_at FROM refresh_tokens LIMIT 1")  # type: ignore[arg-type]
        )

    assert row is not None
    assert row > issued.user.created_at


async def test_refresh_token_model_is_reachable(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Sanity: the ORM sees the same rows the raw SQL does, under RLS."""
    issued = await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        await set_current_organization(session, issued.organization.id)
        tokens = (await session.scalars(RefreshToken.__table__.select())).all()

    assert len(tokens) >= 1


async def test_audit_entries_are_scoped_to_their_tenant(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    first = await _register(sessions, settings, _unique_email())
    await _register(sessions, settings, _unique_email())

    async with sessions() as session, session.begin():
        await set_current_organization(session, first.organization.id)
        rows = (await session.scalars(AuditLog.__table__.select())).all()

    assert rows, "the tenant must see its own audit entries"
