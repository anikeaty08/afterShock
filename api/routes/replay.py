# GET /replay/summary (§10.1). The Replay Lab (§7.5, eval/) hasn't been
# built yet — this honestly reports that rather than fabricating numbers.

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["replay"])


@router.get("/replay/summary")
async def replay_summary() -> dict:
    return {
        "available": False,
        "reason": "The Replay Lab (eval/) has not been built yet — see README.md's status table.",
    }
