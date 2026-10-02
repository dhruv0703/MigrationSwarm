"""Tests for the FastAPI health endpoint."""

import httpx

from migrationswarm.api.main import app


async def test_health_endpoint() -> None:
    """The health endpoint returns the expected service status."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "migrationswarm"}
