# POST /webhooks/github (§10.2). HMAC-verified; the actual PR/merge flow
# runs as a background job (JobRegistry, §10.2 "one job per (pr, head_sha);
# a newer synchronize cancels the older") so the webhook response itself is
# immediate, as GitHub expects.

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from api.github_app import GitHubClient, GitHubContext
from api.github_files import checkout_files_at_ref, list_pr_content_files
from api.jobs import JobRegistry, get_job_registry
from api.report import render_pr_comment
from core.config import get_settings
from core.ingest import changeset_id, run_merge_flow, run_pr_flow
from core.graph import GraphHandle

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])

PULL_REQUEST_TRIGGER_ACTIONS = {"opened", "synchronize", "reopened"}


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> dict:
    settings = get_settings()
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    secret = settings.github_webhook_secret

    if secret:
        from api.github_app import verify_webhook_signature

        if not verify_webhook_signature(body, signature, secret):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    else:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — accepting webhook unverified (dev only)")

    event = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()
    registry = get_job_registry()

    if event == "pull_request":
        return await _handle_pull_request(payload, registry)
    if event == "push":
        # §10.2: the merge flow can be triggered by *either* a merged PR's
        # `pull_request.closed` or a `push` to the default branch. This
        # deployment picks the former (see _handle_pull_request's closed
        # branch) — a bare push with no associated PR has no ChangeSet to
        # look up impacted answers from, so there is nothing more specific
        # to do here than acknowledge it.
        return {"status": "ignored", "reason": "merge flow triggers off pull_request.closed instead"}

    return {"status": "ignored", "event": event}


async def _handle_pull_request(payload: dict, registry: JobRegistry) -> dict:
    action = payload.get("action")
    pr = payload["pull_request"]
    pr_number = pr["number"]
    head_sha = pr["head"]["sha"]
    base_sha = pr["base"]["sha"]
    owner = payload["repository"]["owner"]["login"]
    repo = payload["repository"]["name"]
    installation_id = payload.get("installation", {}).get("id")

    if action not in PULL_REQUEST_TRIGGER_ACTIONS | {"closed"}:
        return {"status": "ignored", "action": action}

    if installation_id is None:
        logger.warning("PR #%d webhook with no installation id — is the GitHub App context missing?", pr_number)
        return {"status": "ignored", "reason": "no installation id"}

    ctx = GitHubContext(
        owner=owner, repo=repo, pr_number=pr_number,
        installation_id=installation_id, head_sha=head_sha, base_sha=base_sha,
    )

    if action == "closed":
        if not pr.get("merged"):
            return {"status": "ignored", "reason": "closed without merging"}
        cs_id = await _latest_changeset_id_for_pr(pr_number)
        if cs_id is None:
            logger.info("PR #%d merged with no prior ChangeSet — nothing to supersede", pr_number)
            return {"status": "ok", "merge_flow": "skipped (no prior ChangeSet)"}
        import asyncio

        asyncio.create_task(_run_merge_flow_job(ctx, cs_id))
        return {"status": "accepted", "job": "merge_flow", "changeset_id": cs_id}

    cs_id = changeset_id(pr_number, head_sha)
    registry.start(pr_number, head_sha, cs_id, lambda: _run_pr_flow_job(ctx, cs_id, registry))
    return {"status": "accepted", "job": "pr_flow", "changeset_id": cs_id}


async def _latest_changeset_id_for_pr(pr: int) -> str | None:
    settings = get_settings()
    graph = GraphHandle(settings.main_graph, settings)
    try:
        rows = await graph.rows(
            "MATCH (cs:ChangeSet {pr: $pr}) RETURN cs.id ORDER BY cs.created_at DESC LIMIT 1",
            {"pr": pr},
        )
        return rows[0][0] if rows else None
    finally:
        await graph.close()


async def _run_pr_flow_job(ctx: GitHubContext, cs_id: str, registry: JobRegistry) -> None:
    settings = get_settings()
    client = GitHubClient(settings)

    with tempfile.TemporaryDirectory(prefix="aftershock-pr-") as tmp:
        checkout_root = Path(tmp)
        added, modified, deleted = await list_pr_content_files(client, ctx)
        await checkout_files_at_ref(client, ctx, added + modified, ctx.head_sha, checkout_root)

        def on_stage(stage: str) -> None:
            registry.progress.publish(cs_id, stage)

        report = await run_pr_flow(
            pr=ctx.pr_number,
            base_sha=ctx.base_sha,
            head_sha=ctx.head_sha,
            added=added,
            modified=modified,
            deleted=deleted,
            checkout_root=checkout_root,
            settings=settings,
            on_stage=on_stage,
        )

    comment_body = render_pr_comment(report)
    await client.upsert_pr_comment(ctx, comment_body)
    await client.create_check_run(ctx, report)


async def _run_merge_flow_job(ctx: GitHubContext, cs_id: str) -> None:
    settings = get_settings()
    client = GitHubClient(settings)
    graph = GraphHandle(settings.main_graph, settings)
    try:
        rows = await graph.rows("MATCH (cs:ChangeSet {id: $id}) RETURN cs.doc_ids", {"id": cs_id})
    finally:
        await graph.close()
    doc_ids = rows[0][0] if rows else []

    with tempfile.TemporaryDirectory(prefix="aftershock-merge-") as tmp:
        checkout_root = Path(tmp)
        # The merge commit's tree already has the final content; re-derive
        # added/modified/deleted from the PR's own file list rather than
        # trusting doc_ids' original op labels, which may be stale if the
        # branch was updated after this ChangeSet was computed.
        added, modified, deleted = await list_pr_content_files(client, ctx)
        await checkout_files_at_ref(client, ctx, added + modified, ctx.head_sha, checkout_root)
        await run_merge_flow(
            pr=ctx.pr_number,
            changeset_id_=cs_id,
            added=added,
            modified=modified,
            deleted=deleted,
            checkout_root=checkout_root,
            settings=settings,
        )
