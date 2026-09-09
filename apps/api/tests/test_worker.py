"""The Celery application and its scheduled tasks (lot L2.4).

Most of what is checked here is configuration, which is unusual for a test
suite and deliberate: every one of these settings is a default that would hurt
if it came back. A regression in `accept_content` is a remote code execution,
not a style problem, and nothing else in the codebase would notice.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.tasks import maintenance
from app.worker import build_celery, run_async

pytestmark = pytest.mark.asyncio

APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)


# --------------------------------------------------------------------------
# The settings that must not drift back
# --------------------------------------------------------------------------


async def test_the_worker_never_accepts_pickle() -> None:
    """Celery's historical default is remote code execution from the broker.

    Anyone able to write to Redis could hand a worker a pickled payload and
    have it executed. No meeting needs pickle; JSON is the whole menu.
    """
    app = build_celery(Settings())

    assert app.conf.accept_content == ["json"]
    assert app.conf.task_serializer == "json"
    assert "pickle" not in app.conf.accept_content


async def test_a_killed_worker_returns_its_task_to_the_queue() -> None:
    """Losing a meeting because a container was rescheduled is not acceptable."""
    app = build_celery(Settings())

    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True


async def test_a_worker_reserves_one_task_at_a_time() -> None:
    """These tasks take minutes; a hoarded batch is a queue that looks stuck."""
    app = build_celery(Settings())

    assert app.conf.worker_prefetch_multiplier == 1


async def test_the_hard_limit_leaves_room_to_fail_cleanly() -> None:
    """A task must get the chance to mark its meeting FAILED before being killed."""
    app = build_celery(Settings())

    assert app.conf.task_time_limit > app.conf.task_soft_time_limit


async def test_the_purge_is_scheduled() -> None:
    """EF-06's promise is only kept if something actually runs."""
    app = build_celery(Settings())

    schedule = app.conf.beat_schedule
    assert "purge-expired-organizations" in schedule
    assert schedule["purge-expired-organizations"]["task"] == "novabrief.purge_organizations"


async def test_the_task_is_registered_under_its_scheduled_name() -> None:
    """A beat entry naming a task nobody registered fails silently, nightly.

    The modules are imported the way a worker imports them, rather than relying
    on this test file having already imported them: the first version of this
    check passed while a real worker refused to start.
    """
    app = build_celery(Settings())
    app.loader.import_default_modules()

    for entry in app.conf.beat_schedule.values():
        assert entry["task"] in app.tasks, f"{entry['task']} is scheduled but not registered"


async def test_the_worker_imports_its_tasks_without_a_cycle() -> None:
    """Caught by a container, not by a test, the first time round.

    Importing app.worker on its own has to be enough to reach every task. In a
    test process app.tasks is usually imported first, which hides a cycle that
    stops a real worker dead.
    """
    import asyncio
    import sys

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from app.worker import celery_app; "
        "celery_app.loader.import_default_modules(); "
        "assert 'novabrief.purge_organizations' in celery_app.tasks",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode()


async def test_the_queue_runs_in_utc() -> None:
    """Two timezones in a scheduler is how a nightly job runs twice, or never."""
    app = build_celery(Settings())

    assert app.conf.enable_utc is True
    assert app.conf.timezone == "UTC"


# --------------------------------------------------------------------------
# The async bridge
# --------------------------------------------------------------------------


async def test_run_async_opens_and_closes_its_own_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task owning its engine is what keeps asyncpg on one event loop."""
    monkeypatch.setenv("DATABASE_URL", APP_DATABASE_URL)
    from app.config import get_settings

    get_settings.cache_clear()

    engine = create_async_engine(APP_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"no test database reachable ({type(exc).__name__})")
    await engine.dispose()

    async def work(session: AsyncSession) -> int:
        value = await session.scalar(text("SELECT 42"))
        return int(value or 0)

    # run_async spins its own loop, so it cannot be awaited from inside one.
    import asyncio

    answer = await asyncio.get_running_loop().run_in_executor(None, run_async, work)

    assert answer == 42
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# The purge task actually purges
# --------------------------------------------------------------------------


async def test_the_purge_task_erases_what_is_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheduled path, not just the service function underneath it."""
    monkeypatch.setenv("DATABASE_URL", APP_DATABASE_URL)
    from app.config import get_settings

    get_settings.cache_clear()

    admin_url = os.environ.get(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql+asyncpg://novabrief:novabrief@localhost:5432/novabrief",
    )
    admin = create_async_engine(admin_url, poolclass=NullPool)
    organization_id = uuid.uuid4()
    try:
        async with admin.connect() as connection:
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, name, deletion_requested_at) "
                    "VALUES (CAST(:id AS uuid), :name, :requested)"
                ),
                {
                    "id": str(organization_id),
                    "name": "Doomed",
                    "requested": datetime.now(UTC) - timedelta(days=8),
                },
            )
            await connection.commit()
    except Exception as exc:  # pragma: no cover - environment dependent
        await admin.dispose()
        pytest.skip(f"no test database reachable ({type(exc).__name__})")

    import asyncio

    purged = await asyncio.get_running_loop().run_in_executor(None, maintenance.purge_organizations)

    try:
        async with admin.connect() as connection:
            still_there = await connection.scalar(
                text("SELECT count(*) FROM organizations WHERE id = CAST(:id AS uuid)"),
                {"id": str(organization_id)},
            )
    finally:
        await admin.dispose()

    assert purged >= 1
    assert still_there == 0
    get_settings.cache_clear()


async def test_the_purge_task_spares_a_live_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", APP_DATABASE_URL)
    from app.config import get_settings

    get_settings.cache_clear()

    admin_url = os.environ.get(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql+asyncpg://novabrief:novabrief@localhost:5432/novabrief",
    )
    admin = create_async_engine(admin_url, poolclass=NullPool)
    organization_id = uuid.uuid4()
    try:
        async with admin.connect() as connection:
            await connection.execute(
                text("INSERT INTO organizations (id, name) VALUES (CAST(:id AS uuid), :name)"),
                {"id": str(organization_id), "name": "Alive"},
            )
            await connection.commit()
    except Exception as exc:  # pragma: no cover - environment dependent
        await admin.dispose()
        pytest.skip(f"no test database reachable ({type(exc).__name__})")

    import asyncio

    await asyncio.get_running_loop().run_in_executor(None, maintenance.purge_organizations)

    try:
        async with admin.connect() as connection:
            still_there = await connection.scalar(
                text("SELECT count(*) FROM organizations WHERE id = CAST(:id AS uuid)"),
                {"id": str(organization_id)},
            )
            await connection.execute(
                text("DELETE FROM organizations WHERE id = CAST(:id AS uuid)"),
                {"id": str(organization_id)},
            )
            await connection.commit()
    finally:
        await admin.dispose()

    assert still_there == 1
    get_settings.cache_clear()
