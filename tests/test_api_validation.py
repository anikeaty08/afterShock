# Pure request-validation tests for the API's Pydantic schemas — no
# FalkorDB or LLM needed, since these fail before reaching any handler.

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_ask_rejects_empty_question():
    resp = client.post("/ask", json={"question": ""})
    assert resp.status_code == 422


def test_ask_rejects_missing_question():
    resp = client.post("/ask", json={})
    assert resp.status_code == 422


def test_impact_preview_rejects_invalid_op():
    resp = client.post(
        "/impact/preview",
        json={"files": [{"path": "docs/x.mdx", "content": "hi", "op": "renamed"}]},
    )
    assert resp.status_code == 422


# Note: a well-formed /impact/preview or /ask body isn't exercised here —
# past validation, the real handler calls run_pr_flow()/answer_question(),
# which makes a real outbound LLM call. Not something a test should trigger
# with no API key configured (it would either fail loudly or, worse,
# actually hit a provider). See tests/test_ingest.py for that pipeline
# tested end-to-end with the LLM boundary faked instead.


def test_replay_summary_reports_unavailable():
    resp = client.get("/replay/summary")
    assert resp.status_code == 200
    assert resp.json()["available"] is False
