# Integration test for core/ingest.py's run_merge_flow (§7.4): re-answering
# the impacted set on docs_main itself and superseding old answers, driven
# by a ChangeSet's IMPACTS edges (as run_pr_flow would have written them).

from __future__ import annotations

from pathlib import Path

from core.config import Settings
from core.graph import GraphHandle
from core.ingest import run_merge_flow
from core.ledger import AnswerWrite, Ledger
from core.reanswer import Answer


async def _fake_apply_changes(rag, *, added, modified, deleted, root, **kwargs):
    from graphrag_sdk import ApplyChangesResult, BatchEntry, UpdateResult

    return ApplyChangesResult(modified=[BatchEntry.ok(UpdateResult()) for _ in modified])


async def _fake_answer_fn(rag, graph: GraphHandle, question: str) -> Answer:
    return Answer(question=question, text="Use the GraphRAG class.", abstained=False, model="fake-model", retriever_result=None, resolved=None)


async def test_run_merge_flow_supersedes_changed_answers(graph_factory, tmp_path: Path):
    settings = Settings(main_graph=(await graph_factory("main")).graph_name, embed_dimensions=8)
    main = GraphHandle(settings.main_graph, settings)
    ledger = Ledger(main, settings)
    await ledger.ensure_indexes()

    old_write = AnswerWrite(
        question_text="Which class builds a knowledge graph?",
        question_embedding=[0.1] * 8,
        answer_text="Use the KnowledgeGraph class.",
        commit_sha="base0000",
        graph_name=main.graph_name,
        abstained=False,
    )
    _, old_answer_id = await ledger.write_answer(old_write)

    cs_id = "pr-479-head123"
    await ledger.persist_changeset(
        cs_id=cs_id, pr=479, base_sha="base0000", head_sha="head1234",
        doc_ids=["commands/graph.copy.mdx"], status="done", timings={},
        touches=[{"doc_id": "commands/graph.copy.mdx", "op": "modified"}],
        impacts=[{
            "answer_id": old_answer_id, "tier": "T1", "via": ["commands/graph.copy.mdx"],
            "verdict": "CHANGED", "new_answer": "Use the GraphRAG class.",
            "reason": "renamed", "score": 1.0,
        }],
    )

    checkout_root = tmp_path / "checkout"
    checkout_root.mkdir()
    (checkout_root / "commands.mdx").write_text("GraphRAG now.", encoding="utf-8")

    summary = await run_merge_flow(
        pr=479,
        changeset_id_=cs_id,
        added=[],
        modified=["commands.mdx"],
        deleted=[],
        checkout_root=checkout_root,
        settings=settings,
        apply_changes_fn=_fake_apply_changes,
        answer_fn=_fake_answer_fn,
    )

    assert len(summary.superseded) == 1
    superseded_old, new_id = summary.superseded[0]
    assert superseded_old == old_answer_id

    rows = await main.rows("MATCH (a:Answer {id:$id}) RETURN a.status", {"id": old_answer_id})
    assert rows[0][0] == "superseded"

    new_rows = await main.rows("MATCH (a:Answer {id:$id}) RETURN a.text, a.status", {"id": new_id})
    assert new_rows[0][0] == "Use the GraphRAG class."
    assert new_rows[0][1] == "current"

    await main.close()
