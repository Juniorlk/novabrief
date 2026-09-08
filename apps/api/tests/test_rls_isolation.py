"""T-07 — multi-tenant isolation. The exit criterion of lot L1.

ADR-04 puts isolation in PostgreSQL rather than in application filters, so this
suite talks to a real database. It is skipped when none is reachable, and the
CI job that runs it has one.

What is being proved is narrow and important: a query that forgets to filter
returns nothing rather than another company's rows. The application filters
stay in place; this is the layer underneath them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import create_session_factory, set_current_organization
from app.models import TENANT_TABLES
from app.uuid7 import uuid7

# The API's own credentials: a restricted role, which is the whole point.
APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)
# The owner's credentials, used only to set fixtures up. Creating a tenant is
# the one operation that cannot itself be scoped to a tenant.
ADMIN_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_ADMIN_URL",
    "postgresql+asyncpg://novabrief:novabrief@localhost:5432/novabrief",
)

pytestmark = pytest.mark.asyncio


async def _factory_or_skip(url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(url, poolclass=None)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"no test database reachable ({type(exc).__name__})")
    return engine, create_session_factory(engine)


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions as the API sees the database: the restricted role."""
    engine, factory = await _factory_or_skip(APP_DATABASE_URL)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def admin_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions as the owner, for creating tenants in fixtures."""
    engine, factory = await _factory_or_skip(ADMIN_DATABASE_URL)
    yield factory
    await engine.dispose()


async def _make_org(session: AsyncSession, name: str) -> uuid.UUID:
    """Insert one organization. Must be called on an admin session."""
    org_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO organizations (id, name, market, plan_code) "
            "VALUES (:id, :name, 'CM', 'free')"
        ),
        {"id": org_id, "name": name},
    )
    return org_id


async def _make_user(session: AsyncSession, org_id: uuid.UUID, email: str) -> uuid.UUID:
    user_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO users (id, organization_id, email, password_hash, full_name) "
            "VALUES (:id, :org, :email, 'x', 'Test User')"
        ),
        {"id": user_id, "org": org_id, "email": email},
    )
    return user_id


async def test_a_tenant_sees_only_its_own_rows(
    sessions: async_sessionmaker[AsyncSession],
    admin_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The core of T-07: no cross-organization read."""
    suffix = uuid.uuid4().hex[:8]

    async with admin_sessions() as session, session.begin():
        org_a = await _make_org(session, f"Alpha {suffix}")
        org_b = await _make_org(session, f"Beta {suffix}")

    async with sessions() as session, session.begin():
        await set_current_organization(session, org_a)
        await _make_user(session, org_a, f"a-{suffix}@test.cm")
    async with sessions() as session, session.begin():
        await set_current_organization(session, org_b)
        await _make_user(session, org_b, f"b-{suffix}@test.cm")

    async with sessions() as session, session.begin():
        await set_current_organization(session, org_a)
        rows = (await session.execute(text("SELECT email FROM users"))).scalars().all()

    assert rows == [f"a-{suffix}@test.cm"], "organization A must not see B's users"


async def test_a_direct_query_without_the_session_variable_returns_nothing(
    sessions: async_sessionmaker[AsyncSession],
    admin_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The second half of T-07: RLS blocks the unscoped SQL query.

    This is the case application filters cannot cover — a psql session, a
    reporting script, a forgotten dependency. It must return zero rows, not
    everything.
    """
    suffix = uuid.uuid4().hex[:8]

    async with admin_sessions() as session, session.begin():
        org = await _make_org(session, f"Gamma {suffix}")
    async with sessions() as session, session.begin():
        await set_current_organization(session, org)
        await _make_user(session, org, f"g-{suffix}@test.cm")

    async with sessions() as session, session.begin():
        # No set_current_organization: the variable is unset.
        rows = (await session.execute(text("SELECT id FROM users"))).scalars().all()

    assert rows == [], "an unscoped query must return nothing, not every tenant"


async def test_a_tenant_cannot_write_a_row_belonging_to_another(
    sessions: async_sessionmaker[AsyncSession],
    admin_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """WITH CHECK, not just USING.

    Reading is only half of isolation: without WITH CHECK a tenant could insert
    a row stamped with someone else's organization_id and quietly plant data
    inside their account.
    """
    suffix = uuid.uuid4().hex[:8]

    async with admin_sessions() as session, session.begin():
        org_a = await _make_org(session, f"Delta {suffix}")
        org_b = await _make_org(session, f"Epsilon {suffix}")

    with pytest.raises(ProgrammingError) as failure:
        async with sessions() as session, session.begin():
            await set_current_organization(session, org_a)
            await _make_user(session, org_b, f"cross-{suffix}@test.cm")

    assert "row-level security" in str(failure.value).lower()


async def test_switching_organization_switches_visibility(
    sessions: async_sessionmaker[AsyncSession],
    admin_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A pooled connection must not carry one tenant's identity to the next.

    SET LOCAL is what guarantees this: the value dies with the transaction.
    """
    suffix = uuid.uuid4().hex[:8]

    async with admin_sessions() as session, session.begin():
        org_a = await _make_org(session, f"Zeta {suffix}")
        org_b = await _make_org(session, f"Eta {suffix}")

    async with sessions() as session, session.begin():
        await set_current_organization(session, org_a)
        await _make_user(session, org_a, f"za-{suffix}@test.cm")
    async with sessions() as session, session.begin():
        await set_current_organization(session, org_b)
        await _make_user(session, org_b, f"zb-{suffix}@test.cm")

    async with sessions() as session, session.begin():
        await set_current_organization(session, org_b)
        seen = (await session.execute(text("SELECT email FROM users"))).scalars().all()
    assert seen == [f"zb-{suffix}@test.cm"]

    # A brand new transaction on a recycled connection starts unscoped.
    async with sessions() as session, session.begin():
        leaked = (await session.execute(text("SELECT email FROM users"))).scalars().all()
    assert leaked == [], "the previous transaction's scope must not survive"


async def test_every_tenant_table_has_rls_enabled_and_forced(
    admin_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A new table must not reach production without a policy.

    FORCE is checked as well as ENABLE: PostgreSQL exempts a table's owner from
    its own policies unless forced, and the application role is usually the
    owner in a small deployment. Enabled-but-not-forced looks protected in
    `\\d` and protects nothing.
    """
    async with admin_sessions() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ),
                {"names": [*TENANT_TABLES, "organizations"]},
            )
        ).all()

    found = {name: (enabled, forced) for name, enabled, forced in rows}
    for table in (*TENANT_TABLES, "organizations"):
        assert table in found, f"{table} is missing from the database"
        enabled, forced = found[table]
        assert enabled, f"{table} does not have row level security enabled"
        assert forced, f"{table} does not force row level security on its owner"
