# Aftershock — API: GitHub App client (§10.2)
#
# Everything the webhook handler and the PR-flow orchestrator need to talk
# to GitHub: HMAC webhook verification, App-JWT -> installation-token
# exchange, the upserted PR comment, and the check run. No GitHub App
# credentials were available in the build environment, so this is written
# and unit-tested against GitHub's documented REST contract (signature
# verification, JWT claims, the comment/check-run payload shapes) rather
# than exercised against a live GitHub App — see AI_USAGE.md.

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

import httpx
import jwt

from core.config import Settings, get_settings
from core.ingest import PRFlowReport

logger = logging.getLogger(__name__)

COMMENT_MARKER = "<!-- aftershock -->"
"""Hidden marker so the PR comment is upserted (§10.2) — found, then PATCHed,
rather than posting a new comment on every `synchronize`."""


def verify_webhook_signature(payload: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub's ``X-Hub-Signature-256`` header.

    Returns False (never raises) for a missing header, a malformed one, or a
    mismatch — the caller's job is uniformly "401 if this is False", not to
    distinguish why.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256=") :]
    return hmac.compare_digest(expected, provided)


def make_app_jwt(app_id: str, private_key_pem: str) -> str:
    """A short-lived (9 min, under GitHub's 10 min cap) App JWT, per GitHub's
    documented claims (iat backdated 60s for clock skew, iss = App ID)."""
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": app_id}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


@dataclass
class GitHubContext:
    """Everything one webhook event's handling needs to address the right
    repo/PR/commit and to authenticate as the right installation."""

    owner: str
    repo: str
    pr_number: int
    installation_id: int
    head_sha: str
    base_sha: str


class GitHubClient:
    """Thin async wrapper over the GitHub REST API calls Aftershock needs.
    Least-privilege by construction: only the endpoints §10.2 lists
    (Contents: read is done by the caller fetching blobs separately; this
    class only ever touches issue comments and check runs)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._installation_tokens: dict[int, tuple[str, int]] = {}
        """installation_id -> (token, expires_at_epoch) — GitHub installation
        tokens last 1h; cached per-process to avoid re-minting one per call."""

    async def _installation_token(self, installation_id: int) -> str:
        cached = self._installation_tokens.get(installation_id)
        if cached and cached[1] > time.time() + 30:
            return cached[0]

        app_id = self.settings.github_app_id
        pem = self.settings.github_private_key_pem()
        if not app_id or not pem:
            raise RuntimeError("GITHUB_APP_ID / GITHUB_PRIVATE_KEY(_PATH) not configured")

        app_jwt = make_app_jwt(app_id, pem)
        url = f"{self.settings.github_api_base}/app/installations/{installation_id}/access_tokens"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        token = data["token"]
        # expires_at is an ISO-8601 string; store as epoch for the cheap
        # comparison above. Fall back to a conservative 55 min if parsing
        # ever disagrees with the documented format.
        try:
            from datetime import datetime

            expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        except Exception:
            expires_at = time.time() + 55 * 60
        self._installation_tokens[installation_id] = (token, expires_at)
        return token

    async def _request(self, method: str, installation_id: int, path: str, **kwargs) -> httpx.Response:
        token = await self._installation_token(installation_id)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            **kwargs.pop("headers", {}),
        }
        async with httpx.AsyncClient(base_url=self.settings.github_api_base) as client:
            resp = await client.request(method, path, headers=headers, **kwargs)
            resp.raise_for_status()
            return resp

    async def get(self, installation_id: int, path: str, **kwargs) -> httpx.Response:
        """Public authenticated GET, for callers outside this module (e.g.
        api/github_files.py's PR-files / contents lookups) that need a raw
        endpoint this class doesn't wrap with its own named method."""
        return await self._request("GET", installation_id, path, **kwargs)

    async def upsert_pr_comment(self, ctx: GitHubContext, body: str) -> int:
        """Find an existing Aftershock comment on the PR (by ``COMMENT_MARKER``)
        and PATCH it; otherwise POST a new one. Returns the comment id."""
        marked_body = f"{COMMENT_MARKER}\n{body}"
        list_path = f"/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.pr_number}/comments"
        resp = await self._request("GET", ctx.installation_id, list_path, params={"per_page": 100})
        for comment in resp.json():
            if COMMENT_MARKER in (comment.get("body") or ""):
                patch_path = f"/repos/{ctx.owner}/{ctx.repo}/issues/comments/{comment['id']}"
                await self._request("PATCH", ctx.installation_id, patch_path, json={"body": marked_body})
                return comment["id"]

        create_resp = await self._request("POST", ctx.installation_id, list_path, json={"body": marked_body})
        return create_resp.json()["id"]

    async def create_check_run(self, ctx: GitHubContext, report: PRFlowReport) -> None:
        """§9 / §10.2: ``neutral`` (GitHub has no literal "warning" conclusion
        — this is the documented convention for that) when any question lost
        all its evidence, ``success`` otherwise."""
        conclusion = "neutral" if report.has_coverage_regression else "success"
        title = (
            "Coverage regression: some answers lost their evidence"
            if report.has_coverage_regression
            else "No coverage regressions"
        )
        summary = (
            f"{len(report.doc_ids)} docs changed · "
            f"{report.facts_added} facts added · {report.facts_removed} removed · {report.facts_modified} modified\n"
            f"{len(report.entries)} answers re-checked · {report.changed_count} changed · "
            f"{report.now_abstains_count} now unanswerable · {report.now_answers_count} newly answerable"
        )
        path = f"/repos/{ctx.owner}/{ctx.repo}/check-runs"
        await self._request(
            "POST",
            ctx.installation_id,
            path,
            json={
                "name": "Aftershock impact report",
                "head_sha": ctx.head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": title, "summary": summary},
            },
        )
