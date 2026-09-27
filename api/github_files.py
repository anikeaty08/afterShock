# Aftershock — API: fetch a PR's changed content files via the GitHub REST
# API and lay them out on disk for core.ingest's apply_pr_changes() (§10.2
# step "list changed files + contents @ head_sha").

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from api.github_app import GitHubClient, GitHubContext
from core.config import Settings

CONTENT_EXTENSIONS = (".md", ".mdx")
"""§7.3 step 1's content-file filter — everything else (images, config,
components) is ignored, and a PR touching none of these still gets a
zero-impact ChangeSet (the Replay Lab's false-positive control case)."""


async def list_pr_content_files(
    client: GitHubClient, ctx: GitHubContext
) -> tuple[list[str], list[str], list[str]]:
    """Returns (added, modified, deleted) repo-relative paths, filtered to
    content files only."""
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    page = 1
    while True:
        resp = await client.get(
            ctx.installation_id,
            f"/repos/{ctx.owner}/{ctx.repo}/pulls/{ctx.pr_number}/files",
            params={"per_page": 100, "page": page},
        )
        files = resp.json()
        if not files:
            break
        for f in files:
            path = f["filename"]
            if not path.lower().endswith(CONTENT_EXTENSIONS):
                continue
            status = f["status"]  # "added" | "modified" | "removed" | "renamed" | ...
            if status == "added":
                added.append(path)
            elif status == "removed":
                deleted.append(path)
            elif status == "renamed":
                # Treat as delete-old + add-new — the SDK layer has no
                # native "rename", and this keeps document_id continuity
                # honest rather than guessing at a rename heuristic.
                deleted.append(f["previous_filename"])
                added.append(path)
            else:
                modified.append(path)
        if len(files) < 100:
            break
        page += 1

    return added, modified, deleted


async def checkout_files_at_ref(
    client: GitHubClient,
    ctx: GitHubContext,
    paths: list[str],
    ref: str,
    dest_root: Path,
) -> None:
    """Fetch each path's content at ``ref`` (typically ``ctx.head_sha``) via
    the Contents API and write it under ``dest_root`` at the same
    repo-relative path, so ``apply_pr_changes(root=dest_root)`` sees exactly
    the layout ``document_id`` expects."""
    for path in paths:
        resp = await client.get(
            ctx.installation_id,
            f"/repos/{ctx.owner}/{ctx.repo}/contents/{path}",
            params={"ref": ref},
        )
        data = resp.json()
        if data.get("encoding") != "base64":
            raise ValueError(f"Unexpected content encoding for {path}: {data.get('encoding')!r}")
        content = base64.b64decode(data["content"])
        out_path = dest_root / path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(content)
