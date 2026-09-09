"""Erase organizations whose retraction window has expired (EF-06).

Run it as `python -m app.jobs.purge_organizations`, once a day. EF-06 promises
that after the seven days nothing survives; this is what keeps the promise, so
a deployment that never schedules it is a deployment that quietly breaks the
commitment.

Celery now schedules the same work nightly (`novabrief.purge_organizations`).
This entry point stays for the case the scheduler is what broke: an operator
needs a way to run the purge by hand without a broker, and both call the same
service function so neither can drift from the other.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.logging import configure_logging, get_logger
from app.services.organizations import purge_due_organizations

logger = get_logger(__name__)


async def run() -> int:
    """Purge what is due and return how many organizations were erased."""
    settings = get_settings()
    configure_logging(settings)

    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        # Unscoped: the job crosses tenants to find what is due, then scopes
        # itself to each one before deleting. One transaction, so a failure
        # halfway leaves nothing half-erased.
        async with factory() as session, session.begin():
            purged = await purge_due_organizations(session)
    finally:
        await engine.dispose()

    logger.info("purge_organizations_finished", purged=len(purged))
    return len(purged)


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(run()) >= 0 else 1)
