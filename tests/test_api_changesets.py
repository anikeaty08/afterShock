# Integration tests for GET /changesets/{id} and /graph, against the real
# configured main_graph (pointed at a throwaway test graph via env var).

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.config import get_settings
from core.graph import GraphHandle
from core.ledger import AnswerWrite, Ledger

client = TestClient(app)


@pytest.fixture
async def seeded_changeset(falkordb_or_skip, monkeypatch):
    import uuid

    graph_name = f"aftershock_test_api_{uuid.uuid4().hex[:12]}"
    get_settings.cache_clear()
    monkeypatch.setenv("MAIN_GRAPH", graph_name)
    get_settings.cache_clear()
    settings = get_settings()

    graph = GraphHandle(graph_name, settings)
    await graph.delete()
    ledger = Ledger(graph, settings)
    await ledger.ensure_indexes()
    write = AnswerWrite(
        question_text="Q?", question_embedding=[0.1] * 8, answer_text="A",
        commit_sha="sha1", graph_name=graph_name, abstained=False,
    )
    _, answer_id = await ledger.write_answer(write)
    cs_id = "pr-1-abc1234"
    await ledger.persist_changeset(
        cs_id=cs_id, pr=1, base_sha="base", head_sha="abc1234",
        doc_ids=["docs/x.mdx"], status="done", timings={"total": 100},
        touches=[{"doc_id": "docs/x.mdx", "op": "modified"}],
        impacts=[{
            "answer_id": answer_id, "tier": "T1", "via": ["docs/x.mdx"],
            "verdict": "CHANGED", "new_answer": "B", "reason": "changed", "score": 1.0,
        }],
    )
    yield cs_id
    await graph.delete()
    await graph.close()
    get_settings.cache_clear()


async def test_get_changeset_returns_persisted_report(seeded_changeset):
    resp = client.get(f"/changesets/{seeded_changeset}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pr"] == 1
    assert body["status"] == "done"
    assert body["doc_ids"] == ["docs/x.mdx"]
    assert len(body["entries"]) == 1
    assert body["entries"][0]["verdict"] == "CHANGED"


async def test_get_changeset_graph_returns_impact_entries(seeded_changeset):
    resp = client.get(f"/changesets/{seeded_changeset}/graph")
    assert resp.status_code == 200
    assert len(resp.json()["entries"]) == 1


def test_get_missing_changeset_is_404(falkordb_or_skip):
    resp = client.get("/changesets/does-not-exist")
    assert resp.status_code == 404
