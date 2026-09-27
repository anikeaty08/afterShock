# Aftershock — Core: Abstention-gated re-answer
#
# §7.2's ask flow and §7.3 step 7's re-answer step are the same operation —
# ask a question against a given graph, abstain if the evidence is too thin,
# resolve the evidence if not. This module is that one operation; ledger.py
# writes the result, judge.py compares two of them.
#
# Follows graphrag_sdk's own documented abstention pattern
# (graphrag_sdk/examples/grounded_answers_with_abstention.py) adapted from
# LocalRetrieval to the default MultiPathRetrieval, per SYSTEM_DESIGN.md
# §7.2: MultiPathRetrieval is what gives T3 its neighbour-traversal paths and
# T2/T4 their fact keys, but its items are whole concatenated *sections*, so
# a CosineReranker's score is a section-level number, not a per-chunk one —
# the threshold in Settings.min_evidence_score is calibrated against that,
# never against a chunk-level number from a different strategy.

from __future__ import annotations

import logging
from dataclasses import dataclass

from graphrag_sdk import GraphRAG, RetrieverResult
from graphrag_sdk.retrieval.reranking_strategies.cosine import CosineReranker

from core.config import Settings, get_settings
from core.evidence import NON_EVIDENCE_SECTIONS, ResolvedEvidence, resolve_evidence
from core.graph import GraphHandle

logger = logging.getLogger(__name__)

INSUFFICIENT_EVIDENCE = "I don't have enough evidence in the knowledge graph to answer that question."
"""Verbatim, stable marker string (mirrors the SDK example) so an abstention
is distinguishable from a real answer without parsing prose — the Replay Lab
oracle (§7.5) and the deterministic judge stage (§9) both key off this."""


@dataclass
class Answer:
    question: str
    text: str
    abstained: bool
    model: str
    retriever_result: RetrieverResult | None
    resolved: ResolvedEvidence | None
    """None exactly when ``abstained`` is True — no generation happened, so
    there is nothing to resolve evidence from."""


def _is_evidence(item, min_score: float | None) -> bool:
    """Same shape as the SDK example's ``is_evidence``, but returns False for
    ``cypher_results`` too, not just ``hint`` — an aggregate row is not
    "evidence" in the citation sense either; it's a computed answer, and Q9/
    Q10 have nothing to resolve it against (see evidence.py's
    NON_EVIDENCE_SECTIONS)."""
    if not (item.content or "").strip():
        return False
    if (item.metadata or {}).get("section") in NON_EVIDENCE_SECTIONS:
        return False
    if min_score is None:
        return True
    return item.score is not None and item.score >= min_score


async def answer_question(
    rag: GraphRAG,
    graph: GraphHandle,
    question: str,
    *,
    settings: Settings | None = None,
) -> Answer:
    """Ask ``question`` of ``rag`` (already bound to ``graph``'s FalkorDB
    graph), abstaining if the evidence is too thin, resolving USED_CHUNK /
    USED_ENTITY / fact_key evidence if not.

    Cost note (from the SDK example this follows): the gate retrieves once
    and ``completion()`` retrieves again internally, and MultiPathRetrieval
    runs an LLM keyword-extraction call on every retrieval — so a refused
    question costs one retrieval's worth of LLM calls, an answered one two
    retrievals' worth plus the generation call. Not free, but still far
    cheaper than generating an answer the graph can't support.
    """
    settings = settings or get_settings()
    reranker = CosineReranker(embedder=rag.embedder, top_k=15)

    retrieved = await rag.retrieve(question, reranker=reranker)
    supporting = [item for item in retrieved.items if _is_evidence(item, settings.min_evidence_score)]

    if len(supporting) < settings.min_evidence_items:
        logger.info("Abstaining (gate): %r — %d/%d items cleared the score floor", question, len(supporting), len(retrieved.items))
        return Answer(
            question=question,
            text=INSUFFICIENT_EVIDENCE,
            abstained=True,
            model=rag.llm.model_name,
            retriever_result=None,
            resolved=None,
        )

    result = await rag.completion(question, reranker=reranker, return_context=True)

    # completion() retrieves again internally, so what it actually generated
    # from can differ from the gate's own retrieval — cite (and resolve
    # evidence from) only what generation itself saw, never substitute the
    # gate's items. If generation's own evidence fails the same bar, abstain:
    # a passing gate does not license an answer generation didn't ground.
    generated_from = result.retriever_result.items if result.retriever_result else []
    cited = [item for item in generated_from if _is_evidence(item, settings.min_evidence_score)]

    if len(cited) < settings.min_evidence_items:
        logger.info("Abstaining (post-generation): %r — generation's own retrieval didn't clear the bar", question)
        return Answer(
            question=question,
            text=INSUFFICIENT_EVIDENCE,
            abstained=True,
            model=rag.llm.model_name,
            retriever_result=None,
            resolved=None,
        )

    resolved = await resolve_evidence(graph, result.retriever_result)
    return Answer(
        question=question,
        text=result.answer,
        abstained=False,
        model=rag.llm.model_name,
        retriever_result=result.retriever_result,
        resolved=resolved,
    )
