# Tests for core/reanswer.py's abstention gate. graphrag_sdk's ``GraphRAG``
# itself is faked out (no LLM key needed / no network) so these exercise
# exactly our own gating logic — the SDK's retrieve()/completion() and
# CosineReranker are its own tested responsibility, not ours to re-verify.
# resolve_evidence's own wiring still runs against the real, live FalkorDB
# (see conftest.py's `graph` fixture) so the "answered" path is a genuine
# integration test, not a fully-mocked one.

from __future__ import annotations

from dataclasses import dataclass, field

from graphrag_sdk.core.models import RetrieverResult, RetrieverResultItem

from core.config import Settings
from core.reanswer import INSUFFICIENT_EVIDENCE, answer_question


class FakeLLM:
    model_name = "fake-model"


class FakeEmbedder:
    """Only used by CosineReranker.rerank(), which our fakes below never
    call for real (see FakeRAG.retrieve/completion) — present so
    CosineReranker(embedder=rag.embedder) can be constructed without error."""

    async def aembed_documents(self, texts, timeout=None):
        return [[0.1, 0.1, 0.1, 0.1] for _ in texts]


@dataclass
class FakeRAG:
    retrieve_result: RetrieverResult
    completion_result: object | None = None
    completion_calls: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.llm = FakeLLM()
        self.embedder = FakeEmbedder()

    async def retrieve(self, question, reranker=None, strategy=None, ctx=None):
        return self.retrieve_result

    async def completion(self, question, reranker=None, return_context=False, **kwargs):
        self.completion_calls.append(question)
        return self.completion_result


def _item(section: str, content: str = "some content", score: float | None = None) -> RetrieverResultItem:
    return RetrieverResultItem(content=content, metadata={"section": section}, score=score)


class FakeRagResult:
    def __init__(self, answer: str, retriever_result: RetrieverResult | None):
        self.answer = answer
        self.retriever_result = retriever_result


async def test_gate_abstains_when_no_item_clears_the_score_floor():
    settings = Settings(min_evidence_items=1, min_evidence_score=0.30)
    retrieve_result = RetrieverResult(items=[_item("passages", score=0.10)])
    rag = FakeRAG(retrieve_result=retrieve_result)

    # graph is never touched on the abstain path — pass a bare object; a
    # real GraphHandle isn't needed since resolve_evidence must not be called.
    result = await answer_question(rag, graph=object(), question="unanswerable?", settings=settings)

    assert result.abstained is True
    assert result.text == INSUFFICIENT_EVIDENCE
    assert result.resolved is None
    assert rag.completion_calls == [], "completion() must not run once the gate has already fired"


async def test_gate_abstains_when_only_hint_or_cypher_results_present():
    """hint/cypher_results carry no document evidence (evidence.py's
    NON_EVIDENCE_SECTIONS) — a retrieval that returns only those must gate
    exactly like an empty one, regardless of score."""
    settings = Settings(min_evidence_items=1, min_evidence_score=0.0)
    retrieve_result = RetrieverResult(
        items=[
            _item("hint", content="Answer format: ...", score=1.0),
            _item("cypher_results", content="- 3", score=1.0),
        ]
    )
    rag = FakeRAG(retrieve_result=retrieve_result)

    result = await answer_question(rag, graph=object(), question="how many?", settings=settings)
    assert result.abstained is True


async def test_post_generation_abstain_when_completion_retrieval_disagrees():
    """The gate passes, but completion()'s own (separate) retrieval comes
    back thin — must still abstain rather than report an ungrounded answer."""
    settings = Settings(min_evidence_items=1, min_evidence_score=0.30)
    gate_retrieve = RetrieverResult(items=[_item("passages", score=0.90)])
    thin_completion_retrieve = RetrieverResult(items=[_item("passages", score=0.05)])
    rag = FakeRAG(
        retrieve_result=gate_retrieve,
        completion_result=FakeRagResult("some answer", thin_completion_retrieve),
    )

    result = await answer_question(rag, graph=object(), question="q?", settings=settings)
    assert result.abstained is True
    assert result.text == INSUFFICIENT_EVIDENCE


async def test_answers_and_resolves_evidence_when_both_gates_pass(graph):
    await graph.run(
        """
        CREATE (d:Document {id:'docs/a.mdx'})-[:PART_OF]->(c:Chunk {id:'c1', text:'FalkorDB supports GRAPH.COPY.'})
        CREATE (e1:__Entity__ {id:'falkordb__product', name:'FalkorDB'})-[:MENTIONED_IN]->(c)
        CREATE (e2:__Entity__ {id:'graph.copy__command', name:'GRAPH.COPY'})-[:MENTIONED_IN]->(c)
        CREATE (e1)-[:RELATES {rel_type:'SUPPORTS', fact:'FalkorDB supports GRAPH.COPY'}]->(e2)
        """
    )
    settings = Settings(min_evidence_items=1, min_evidence_score=0.30)
    passages_content = "## Source Document Passages\n[Source: docs/a.mdx]\nFalkorDB supports GRAPH.COPY."
    facts_content = "## Knowledge Graph Facts\n- FalkorDB —[SUPPORTS]→ GRAPH.COPY: FalkorDB supports GRAPH.COPY"
    good_retrieve = RetrieverResult(
        items=[_item("passages", content=passages_content, score=0.90), _item("facts", content=facts_content, score=0.85)]
    )
    rag = FakeRAG(
        retrieve_result=good_retrieve,
        completion_result=FakeRagResult("Yes, FalkorDB supports GRAPH.COPY.", good_retrieve),
    )

    result = await answer_question(rag, graph, "Does FalkorDB support GRAPH.COPY?", settings=settings)

    assert result.abstained is False
    assert result.text == "Yes, FalkorDB supports GRAPH.COPY."
    assert result.resolved is not None
    assert "docs/a.mdx" in result.resolved.doc_ids
    assert "chunk-1" not in result.resolved.chunk_ids  # sanity: no stray id invented
    assert "c1" in result.resolved.chunk_ids
    assert "falkordb__product|SUPPORTS|graph.copy__command" in result.resolved.fact_keys
