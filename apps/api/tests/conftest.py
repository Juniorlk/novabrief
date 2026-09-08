"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import create_app
from app.ratelimit import InMemoryRateLimiter


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Stop configuration leaking between tests.

    Settings are cached for the life of the process, so a test that changes the
    environment would otherwise be visible to the ones after it.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client wired to the app, with no network and no database."""
    # No Redis in these tests: they exercise routing and error shapes, and a
    # real client would leave sockets open for the next test to trip over.
    app = create_app(limiter=InMemoryRateLimiter())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
