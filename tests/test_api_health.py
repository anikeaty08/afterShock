# Integration test for GET /health.

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app


async def test_health_reports_falkordb_reachable(falkordb_or_skip):
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["falkordb"] is True
    assert "main_graph" in body
