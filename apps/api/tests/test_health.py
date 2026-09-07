"""The exit criterion of lot L0: the API answers on /health."""

from __future__ import annotations

from httpx import AsyncClient

from app.middleware import DEBUG_ID_HEADER


async def test_health_reports_the_process_is_up(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


async def test_health_does_not_depend_on_the_database(client: AsyncClient) -> None:
    """Liveness must not touch a dependency.

    No database is running in this test. If /health needed one, an orchestrator
    would restart a perfectly healthy API every time PostgreSQL hiccuped.
    """
    response = await client.get("/health")

    assert response.status_code == 200


async def test_every_response_carries_a_debug_id(client: AsyncClient) -> None:
    """ADR-07: a caller can always quote an identifier to support."""
    response = await client.get("/health")

    debug_id = response.headers.get(DEBUG_ID_HEADER)
    assert debug_id is not None
    assert debug_id.startswith("DBG-")


async def test_a_client_supplied_debug_id_is_kept(client: AsyncClient) -> None:
    """A meeting keeps one identity from the desktop app through the API."""
    response = await client.get("/health", headers={DEBUG_ID_HEADER: "DBG-DSK-20260907-4242"})

    assert response.headers[DEBUG_ID_HEADER] == "DBG-DSK-20260907-4242"


async def test_a_hostile_debug_id_is_replaced_not_echoed(client: AsyncClient) -> None:
    """A client-supplied identifier reaches the logs, so it is not trusted."""
    response = await client.get("/health", headers={DEBUG_ID_HEADER: "bad\nvalue with spaces"})

    debug_id = response.headers[DEBUG_ID_HEADER]
    assert debug_id.startswith("DBG-")
    assert "\n" not in debug_id


async def test_readiness_reports_degraded_without_a_database(client: AsyncClient) -> None:
    """Readiness does depend on the database, and says so when it is missing."""
    response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert "database" in body["checks"]


async def test_unknown_routes_return_problem_details(client: AsyncClient) -> None:
    """RFC 9457 everywhere, with the debug_id that ties it to the logs."""
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["status"] == 404
    assert body["debug_id"].startswith("DBG-")
    assert body["instance"] == "/does-not-exist"
