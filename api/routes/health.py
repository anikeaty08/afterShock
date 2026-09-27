# GET /health (§10.1) — FalkorDB + LLM reachability.

from __future__ import annotations

from fastapi import APIRouter

from api.schemas import HealthResponse
from core.config import get_settings
from core.graph import GraphHandle

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    graph = GraphHandle(settings.main_graph, settings)
    try:
        alive = await graph.ping()
    except Exception as exc:
        alive = False
        detail = {"error": str(exc)}
    else:
        detail = {}
    finally:
        await graph.close()

    return HealthResponse(
        falkordb=alive,
        main_graph=settings.main_graph,
        demo_mode=settings.demo_mode,
        detail=detail,
    )
