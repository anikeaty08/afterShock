# Integration tests for core/ledger.py against a live FalkorDB (see
# tests/conftest.py — skipped automatically if FalkorDB isn't reachable).

from __future__ import annotations

from core.graph import GraphHandle
from core.ledger import AnswerWrite, Ledger, question_id_for


async def _seed_sdk_layer(g: GraphHandle) -> None:
    """One Document/Chunk/Entity/RELATES quad — just enough SDK-layer graph
    for write_answer()'s USED_CHUNK / USED_ENTITY edges to attach to."""
    await g.run(
        """
        CREATE (d:Document {id:'docs/a.mdx', path:'docs/a.mdx'})
        CREATE (c:Chunk {id:'chunk1', text:'FalkorDB supports GRAPH.COPY for fast duplication.'})
        CREATE (d)-[:PART_OF]->(c)
        CREATE (e1:__Entity__ {id:'falkordb__product', name:'FalkorDB'})
        CREATE (e2:__Entity__ {id:'graph.copy__command', name:'GRAPH.COPY'})
        CREATE (e1)-[:RELATES {rel_type:'SUPPORTS', fact:'FalkorDB supports GRAPH.COPY'}]->(e2)
        """
    )


async def test_ensure_indexes_is_idempotent(graph: GraphHandle):
    ledger = Ledger(graph)
    await ledger.ensure_indexes()
    await ledger.ensure_indexes()  # must not raise the second time (idempotent)

    # db.indexes() groups every indexed property of one label into a single
    # row, so this is 3 rows (ChangeSet, Answer, Question), not 5 — one per
    # label carrying at least one of §6.3's indexed properties.
    rows = await graph.rows("CALL db.indexes()")
    by_label = {r[0]: dict(r[2]) for r in rows}
    assert set(by_label) == {"ChangeSet", "Answer", "Question"}
    assert by_label["ChangeSet"]["id"] == ["RANGE"]
    assert by_label["Answer"]["id"] == ["RANGE"]
    assert by_label["Answer"]["status"] == ["RANGE"]
    assert by_label["Question"]["id"] == ["RANGE"]
    assert by_label["Question"]["embedding"] == ["VECTOR"]


async def test_write_answer_roundtrip(graph: GraphHandle):
    ledger = Ledger(graph)
    await ledger.ensure_indexes()
    await _seed_sdk_layer(graph)

    w = AnswerWrite(
        question_text="Does FalkorDB support GRAPH.COPY?",
        question_embedding=[0.1] * 8,
        answer_text="Yes, FalkorDB supports GRAPH.COPY.",
        commit_sha="abc123",
        graph_name=graph.graph_name,
        abstained=False,
        fact_keys=["falkordb__product|SUPPORTS|graph.copy__command"],
        entity_ids=["falkordb__product", "graph.copy__command"],
        doc_ids=["docs/a.mdx"],
        chunk_ids=["chunk1"],
        model="test-model",
    )
    qid, aid = await ledger.write_answer(w)
    assert qid == question_id_for("Does FalkorDB support GRAPH.COPY?")

    cur = await ledger.current_answer_for_question(aid)
    assert cur is not None
    assert cur["question_text"] == "Does FalkorDB support GRAPH.COPY?"
    assert cur["fact_keys"] == w.fact_keys

    # as_of(None) must ask the server for "now" (see the docstring in
    # ledger.py) rather than the test process's own clock, or a WSL2-style
    # clock skew between this process and the FalkorDB container can hide a
    # row that was, in real wall-clock time, already written.
    as_of = await ledger.as_of(qid)
    assert as_of is not None
    assert as_of["text"] == w.answer_text

    all_q = await ledger.all_current_questions()
    assert len(all_q) == 1
    assert all_q[0]["question_id"] == qid


async def test_changeset_persist_and_impact_subgraph(graph: GraphHandle):
    ledger = Ledger(graph)
    await ledger.ensure_indexes()
    await _seed_sdk_layer(graph)
    w = AnswerWrite(
        question_text="Q?",
        question_embedding=[0.1] * 8,
        answer_text="A",
        commit_sha="sha1",
        graph_name=graph.graph_name,
        abstained=False,
    )
    _, aid = await ledger.write_answer(w)

    cs_id = "pr-1-abc1234"
    await ledger.set_changeset_status(cs_id, 1, "abc1234", "running", "diff")
    await ledger.persist_changeset(
        cs_id=cs_id,
        pr=1,
        base_sha="base1",
        head_sha="abc1234",
        doc_ids=["docs/a.mdx"],
        status="done",
        timings={"ms": 12},
        touches=[{"doc_id": "docs/a.mdx", "op": "modified"}],
        impacts=[
            {
                "answer_id": aid,
                "tier": "T1",
                "via": ["docs/a.mdx"],
                "verdict": "CHANGED",
                "new_answer": "new text",
                "reason": "api renamed",
                "score": 1.0,
            }
        ],
    )
    sub = await ledger.impact_subgraph(cs_id)
    assert len(sub) == 1
    assert sub[0]["tier"] == "T1"
    assert sub[0]["verdict"] == "CHANGED"


async def test_supersede_marks_old_answer_superseded(graph: GraphHandle):
    ledger = Ledger(graph)
    await graph.run(
        "CREATE (:Answer {id:'a-old', status:'current'}) CREATE (:Answer {id:'a-new', status:'current'})"
    )
    await ledger.supersede("a-old", "a-new", pr=1, verdict="CHANGED")
    rows = await graph.rows("MATCH (a:Answer {id:'a-old'}) RETURN a.status")
    assert rows[0][0] == "superseded"
