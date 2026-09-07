"""Database access, with tenant isolation built into the session.

ADR-04 puts multi-tenant isolation in PostgreSQL Row-Level Security, *in
addition to* application filters rather than instead of them. A forgotten
`WHERE organization_id = ...` is then a bug that returns nothing, not a bug
that leaks another company's meetings.

The mechanism is one setting per transaction: `SET LOCAL app.current_org_id`.
`SET LOCAL` is what makes it safe with a connection pool — the value dies with
the transaction, so a pooled connection can never carry one tenant's identity
into another tenant's query.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings

# The session variable RLS policies read. Named once so a typo cannot silently
# disable isolation in one place while leaving it on everywhere else.
ORG_SETTING = "app.current_org_id"


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for the configured database."""
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        # SQL statements can embed identifiers but never meeting content; echo
        # stays off outside local debugging regardless.
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to `engine`."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def set_current_organization(session: AsyncSession, organization_id: UUID) -> None:
    """Scope the current transaction to one organization.

    `SET LOCAL` is deliberate: the value is reverted when the transaction ends,
    so a connection returned to the pool cannot leak this tenant's identity into
    the next request. A session-wide `SET` would do exactly that.

    The identifier is bound as a parameter rather than interpolated: it arrives
    from a token and must never be able to alter the statement.
    """
    await session.execute(
        text(f"SELECT set_config('{ORG_SETTING}', :org_id, true)"),
        {"org_id": str(organization_id)},
    )


@asynccontextmanager
async def organization_scope(
    session_factory: async_sessionmaker[AsyncSession], organization_id: UUID
) -> AsyncIterator[AsyncSession]:
    """Open a transaction already scoped to one organization.

    Every read and write inside the block is filtered by RLS. Use this rather
    than opening a session by hand: an unscoped session sees nothing, which
    fails loudly, but the habit of opening one is what eventually produces a
    query run under a privileged path.
    """
    async with session_factory() as session, session.begin():
        await set_current_organization(session, organization_id)
        yield session
