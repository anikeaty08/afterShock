# POST /impact/preview (§10.1, §10.3). A dry run on uncommitted content —
# no real PR, so no permanent ChangeSet is written (run_pr_flow(persist=False)).
# This is what mcp_server's preview_impact tool wraps.

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from fastapi import APIRouter

from api.schemas import ImpactedAnswer, ImpactPreviewRequest, ImpactPreviewResponse
from core.config import get_settings
from core.ingest import run_pr_flow

router = APIRouter(tags=["impact"])


@router.post("/impact/preview", response_model=ImpactPreviewResponse)
async def preview_impact(body: ImpactPreviewRequest) -> ImpactPreviewResponse:
    settings = get_settings()
    added = [f.path for f in body.files if f.op == "added"]
    modified = [f.path for f in body.files if f.op == "modified"]
    deleted = [f.path for f in body.files if f.op == "deleted"]

    # A synthetic, content-addressed "PR" identity: pr=0 is reserved for
    # previews, and the pseudo head_sha keeps concurrent previews from
    # colliding on the same scratch graph name (scratch_graph_name(pr, sha)).
    digest = hashlib.sha256(
        "".join(f"{f.op}:{f.path}:{f.content}" for f in body.files).encode("utf-8")
    ).hexdigest()

    with tempfile.TemporaryDirectory(prefix="aftershock-preview-") as tmp:
        checkout_root = Path(tmp)
        for f in body.files:
            if f.op == "deleted":
                continue
            out_path = checkout_root / f.path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(f.content, encoding="utf-8")

        report = await run_pr_flow(
            pr=0,
            base_sha="preview",
            head_sha=digest,
            added=added,
            modified=modified,
            deleted=deleted,
            checkout_root=checkout_root,
            settings=settings,
            persist=False,
        )

    return ImpactPreviewResponse(
        facts_added=report.facts_added,
        facts_removed=report.facts_removed,
        facts_modified=report.facts_modified,
        tier_raw_counts=report.tier_raw_counts,
        truncated=report.truncated,
        impacted=[
            ImpactedAnswer(
                answer_id=e.answer_id, question=e.question, tier=e.tier, via=e.via,
                verdict=e.verdict, old_answer=e.old_answer, new_answer=e.new_answer,
                reason=e.reason, score=e.score,
            )
            for e in report.entries
        ],
        timings_ms=report.timings_ms,
    )
