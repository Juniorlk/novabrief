"""Celery tasks.

Each task is a thin wrapper: it opens a transaction and calls a service
function. The rules live in `app.services`, so what runs in a worker is the
same code the tests exercise directly, not a second implementation that can
drift from the first.
"""

from app.tasks import maintenance

__all__ = ["maintenance"]
