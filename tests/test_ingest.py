# End-to-end integration test for core/ingest.py's run_pr_flow (§7.3).
#
# The two LLM-touching seams (apply_changes_fn, answer_fn/judge_llm) are
# faked so this needs no API key, but everything else — GRAPH.COPY, fact
# diff, impact tiers, ledger persistence — runs for real against FalkorDB.
# The fake apply_changes_fn mutates the scratch graph directly via Cypher,
# standing in for what a real ingest()/update() call would have produced.

from __future__ import annotations

import json
from pathlib import Path

import pytest
from graphrag_sdk import ApplyChangesResult, BatchEntry, UpdateResult

from core.config import Settings
from core.graph import GraphHandle
from core.ingest import run_pr_flow
from core.ledger import AnswerWrite, Ledger
from core.reanswer import Answer

DOC = "commands/graph.copy.mdx"


async def _seed_docs_main(main: GraphHandle) -> tuple[str, str]:
    """A tiny docs_main: one document, the KnowledgeGraph->GraphRAG-shaped
    entities, and two ledger answers — one that will be T1-impacted (cited
    the changed doc) and one that's a control (untouched by the PR)."""
    await Ledger(main).ensure_indexes()
    await main.run(
        """
        CREATE (d:Document {id: $doc})
        CREATE (c:Chunk {id:'chunk-old', text:'KnowledgeGraph builds a knowledge graph.'})
        CREATE (d)-[:PART_OF]->(c)
        CREATE (sdk:__Entity__ {id:'graphrag_sdk__product', name:'GraphRAG SDK'})-[:MENTIONED_IN]->(c)
        CREATE (kg:__Entity__ {id:'knowledgegraph__apiclass', name:'KnowledgeGraph'})-[:MENTIONED_IN]->(c)
        CREATE (sdk)-[:RELATES {rel_type:'SUPPORTS', fact:'GraphRAG SDK KnowledgeGraph builds a knowledge graph'}]->(kg)
        """,
        {"doc": DOC},
    )

    write = AnswerWrite(
        question_text="Which class builds a knowledge graph?",
        question_embedding=[0.1] * 8,
        answer_text="Use the KnowledgeGraph class.",
        commit_sha="base0000",
        graph_name=main.graph_name,
        abstained=False,
        fact_keys=["graphrag_sdk__product|SUPPORTS|knowledgegraph__apiclass"],
        entity_ids=["graphrag_sdk__product", "knowledgegraph__apiclass"],
        doc_ids=[DOC],
        chunk_ids=["chunk-old"],
        model="fake-model",
    )
    _, impacted_answer_id = await Ledger(main).write_answer(write)

    control_write = AnswerWrite(
        question_text="What port does FalkorDB use by default?",
        question_embedding=[0.9] * 8,
        answer_text="6379.",
        commit_sha="base0000",
        graph_name=main.graph_name,
        abstained=False,
    )
    _, control_answer_id = await Ledger(main).write_answer(control_write)

    return impacted_answer_id, control_answer_id


async def _fake_apply_changes(rag, *, added, modified, deleted, root, **kwargs) -> ApplyChangesResult:
    """Stands in for a real ingest()/update() call: rewrites the *scratch*
    graph's KnowledgeGraph entity into GraphRAG, exactly like the real PR
    content (read from ``root``) describes -- but via Cypher instead of an
    LLM extraction pass."""
    graph_name = rag._conn.config.graph_name  # the scratch graph this rag is bound to
    scratch = GraphHandle(graph_name, Settings())
    # Sanity: the fake reads the same file content run_pr_flow wrote to disk,
    # proving `root` is wired through correctly end to end.
    content = (root / modified[0]).read_text(encoding="utf-8")
    assert "GraphRAG" in content

    await scratch.run(
        """
        MATCH (kg:__Entity__ {id:'knowledgegraph__apiclass'})
        DETACH DELETE kg
        WITH 1 AS _
        MATCH (sdk:__Entity__ {id:'graphrag_sdk__product'})-[:MENTIONED_IN]->(c:Chunk)
        CREATE (gr:__Entity__ {id:'graphrag__apiclass', name:'GraphRAG'})-[:MENTIONED_IN]->(c)
        CREATE (sdk)-[:RELATES {rel_type:'SUPPORTS', fact:'GraphRAG SDK GraphRAG class builds a knowledge graph'}]->(gr)
        """
    )
    await scratch.close()
    return ApplyChangesResult(modified=[BatchEntry.ok(UpdateResult())])


async def _fake_answer_fn(rag, graph: GraphHandle, question: str) -> Answer:
    if "builds a knowledge graph" in question:
        return Answer(
            question=question, text="Use the GraphRAG class.", abstained=False,
            model="fake-model", retriever_result=None, resolved=None,
        )
    # The control question's answer is unaffected by the PR.
    return Answer(
        question=question, text="6379.", abstained=False,
        model="fake-model", retriever_result=None, resolved=None,
    )


class _FakeJudgeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeJudgeLLM:
    async def ainvoke(self, prompt, **kwargs):
        return _FakeJudgeResponse(
            '{"verdict": "CHANGED", "changed_claims": '
            '[{"old": "KnowledgeGraph", "new": "GraphRAG"}], '
            '"reason": "The SDK renamed KnowledgeGraph to GraphRAG."}'
        )


async def test_run_pr_flow_end_to_end(graph_factory, tmp_path: Path):
    settings = Settings(main_graph=(await graph_factory("main")).graph_name, embed_dimensions=8)
    main = GraphHandle(settings.main_graph, settings)
    impacted_id, control_id = await _seed_docs_main(main)

    checkout_root = tmp_path / "checkout"
    (checkout_root / "commands").mkdir(parents=True)
    (checkout_root / DOC).write_text(
        "GraphRAG builds a knowledge graph from raw text.", encoding="utf-8"
    )

    report = await run_pr_flow(
        pr=479,
        base_sha="base0000",
        head_sha="head1234",
        added=[],
        modified=[DOC],
        deleted=[],
        checkout_root=checkout_root,
        settings=settings,
        apply_changes_fn=_fake_apply_changes,
        answer_fn=_fake_answer_fn,
        judge_llm=_FakeJudgeLLM(),
    )

    assert report.pr == 479
    assert report.doc_ids == [DOC]
    assert report.facts_removed == 1  # old SUPPORTS->KnowledgeGraph fact
    assert report.facts_added == 1  # new SUPPORTS->GraphRAG fact
    assert report.truncated is False

    by_id = {e.answer_id: e for e in report.entries}
    assert impacted_id in by_id
    assert by_id[impacted_id].tier == "T1"
    assert by_id[impacted_id].verdict == "CHANGED"
    assert by_id[impacted_id].new_answer == "Use the GraphRAG class."
    assert control_id not in by_id, "control question must not be impacted"
    assert report.changed_count == 1
    assert report.has_coverage_regression is False

    # Persisted to the ledger, independently readable back (Q15).
    ledger = Ledger(main, settings)
    subgraph = await ledger.impact_subgraph(report.changeset_id)
    assert len(subgraph) == 1
    assert subgraph[0]["verdict"] == "CHANGED"

    # The scratch graph is cleaned up by default.
    from core.graph import list_graphs

    names = await list_graphs(settings)
    assert not any(n.startswith("docs_pr_479_") for n in names)

    await main.close()
