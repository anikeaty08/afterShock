# GET /changesets/{id}, /graph, /events (§10.1).

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from api.jobs import get_job_registry
from core.config import get_settings
from core.graph import GraphHandle
from core.ledger import Ledger

router = APIRouter(prefix="/changesets", tags=["changesets"])


@router.get("/{changeset_id}")
async def get_changeset(changeset_id: str) -> dict:
    settings = get_settings()
    graph = GraphHandle(settings.main_graph, settings)
    try:
        rows = await graph.rows(
            "MATCH (cs:ChangeSet {id: $id}) RETURN cs.pr, cs.base_sha, cs.head_sha, "
            "cs.doc_ids, cs.status, cs.timings, cs.stage",
            {"id": changeset_id},
        )
        if not rows:
            raise HTTPException(status_code=404, detail="ChangeSet not found")
        pr, base_sha, head_sha, doc_ids, status, timings_json, stage = rows[0]
        ledger = Ledger(graph, settings)
        entries = await ledger.impact_subgraph(changeset_id)
        return {
            "changeset_id": changeset_id,
            "pr": pr,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "doc_ids": doc_ids,
            "status": status,
            "stage": stage,
            "timings": json.loads(timings_json) if timings_json else {},
            "entries": entries,
        }
    finally:
        await graph.close()


@router.get("/{changeset_id}/graph")
async def get_changeset_graph(changeset_id: str) -> dict:
    """Q15 — nodes/edges for the impact graph UI (ChangeSet -> Answer <-
    Question, with the documents each answer's evidence touches)."""
    settings = get_settings()
    graph = GraphHandle(settings.main_graph, settings)
    try:
        ledger = Ledger(graph, settings)
        entries = await ledger.impact_subgraph(changeset_id)
        if not entries:
            raise HTTPException(status_code=404, detail="ChangeSet not found or has no impacts")
        return {"changeset_id": changeset_id, "entries": entries}
    finally:
        await graph.close()


@router.get("/{changeset_id}/events")
async def changeset_events(changeset_id: str):
    """SSE progress: copy -> apply -> diff -> impact -> re-answer -> judge ->
    post. One event per stage transition; the stream closes when the job
    finishes (JobRegistry pushes the sentinel — see api/jobs.py)."""
    registry = get_job_registry()
    queue = registry.progress.subscribe(changeset_id)

    async def event_generator():
        try:
            while True:
                stage = await queue.get()
                if stage is None:
                    yield {"event": "done", "data": "done"}
                    break
                yield {"event": "stage", "data": stage}
        except asyncio.CancelledError:
            pass
        finally:
            registry.progress.unsubscribe(changeset_id, queue)

    return EventSourceResponse(event_generator())
