# POST /ask (§10.1, §7.2 ask flow).

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request

from api.schemas import AskRequest, AskResponse, EvidenceItem
from core.config import get_settings
from core.graph import GraphHandle
from core.ingest import build_rag
from core.ledger import AnswerWrite, Ledger
from core.reanswer import answer_question

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ask"])

# §14: rate-limit /ask. Single-process, fixed-window per client IP — the
# same "good enough for one instance, document the limit" tradeoff as
# api/jobs.py's progress fan-out; a multi-worker deployment needs a shared
# store (Redis/FalkorDB) instead.
_request_log: dict[str, list[float]] = {}


def _rate_limited(client_ip: str, limit_per_minute: int) -> bool:
    now = time.monotonic()
    window_start = now - 60
    hits = [t for t in _request_log.get(client_ip, []) if t >= window_start]
    hits.append(now)
    _request_log[client_ip] = hits
    return len(hits) > limit_per_minute


@router.post("/ask", response_model=AskResponse)
async def ask(request: Request, body: AskRequest) -> AskResponse:
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip, settings.ask_rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="Too many requests")

    graph = GraphHandle(settings.main_graph, settings)
    rag = build_rag(settings.main_graph, settings=settings)
    try:
        result = await answer_question(rag, graph, body.question, settings=settings)

        # Always embed the question, even on abstention (§6.3): the
        # Question vector index (T4's reverse retrieval, Q6) needs every
        # question present, abstained or not — a docs change might make a
        # previously-unanswerable question newly reachable.
        question_embedding = (await rag.embedder.aembed_documents([body.question]))[0]

        ledger = Ledger(graph, settings)
        write = AnswerWrite(
            question_text=body.question,
            question_embedding=question_embedding,
            answer_text=result.text,
            commit_sha="",
            graph_name=settings.main_graph,
            abstained=result.abstained,
            fact_keys=sorted(result.resolved.fact_keys) if result.resolved else [],
            entity_ids=sorted(result.resolved.entity_ids) if result.resolved else [],
            doc_ids=sorted(result.resolved.doc_ids) if result.resolved else [],
            chunk_ids=sorted(result.resolved.chunk_ids) if result.resolved else [],
            model=result.model,
            source="live",
        )
        question_id, answer_id = await ledger.write_answer(write)

        evidence = []
        if result.resolved:
            for fact_key in sorted(result.resolved.fact_keys):
                evidence.append(EvidenceItem(fact_key=fact_key))
            for chunk_id in sorted(result.resolved.chunk_ids):
                evidence.append(EvidenceItem(chunk_id=chunk_id))
            for doc_id in sorted(result.resolved.doc_ids):
                evidence.append(EvidenceItem(doc=doc_id))

        return AskResponse(
            answer_id=answer_id,
            question_id=question_id,
            answer=result.text,
            abstained=result.abstained,
            evidence=evidence,
        )
    finally:
        await graph.close()
