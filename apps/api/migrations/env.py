"""Alembic environment.

The database URL comes from the application settings rather than alembic.ini,
so migrations and the API can never disagree about which database they mean,
and no credential is committed.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares the live database against this metadata.
target_metadata = Base.metadata

# Migrations run with the admin credentials when they are configured: the
# application role deliberately cannot create tables or policies.
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.database_admin_url or _settings.database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review before applying."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations on an established connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Column type changes are otherwise missed by autogenerate, which makes
        # a migration silently incomplete.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply the migrations against the configured database."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # Migrations are a short-lived process; pooling would only hold
        # connections open after the work is done.
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
