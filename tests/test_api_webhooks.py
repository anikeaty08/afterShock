# Tests for POST /webhooks/github: signature enforcement and event dispatch
# routing. JobRegistry.start is monkeypatched to a spy so a dispatched job
# never actually runs (no real GitHub API / LLM calls from a test) — the
# full pipeline is already covered end-to-end in tests/test_ingest.py with
# the LLM boundary faked.

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from api.jobs import JobRegistry
from api.main import app
from core.config import get_settings

client = TestClient(app)


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    yield
    get_settings.cache_clear()


def _pr_payload(action: str, pr_number: int = 479, merged: bool = False) -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "merged": merged,
            "head": {"sha": "head1234"},
            "base": {"sha": "base0000"},
        },
        "repository": {"owner": {"login": "FalkorDB"}, "name": "docs"},
        "installation": {"id": 999},
    }


class TestSignatureEnforcement:
    def test_missing_signature_is_rejected(self):
        body = json.dumps(_pr_payload("opened")).encode()
        resp = client.post(
            "/webhooks/github", content=body,
            headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
        )
        assert resp.status_code == 401

    def test_wrong_signature_is_rejected(self):
        body = json.dumps(_pr_payload("opened")).encode()
        resp = client.post(
            "/webhooks/github", content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": _sign(body, "wrong-secret"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_correct_signature_is_accepted(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            JobRegistry, "start",
            lambda self, pr, head_sha, cs_id, coro_factory: calls.append((pr, head_sha, cs_id)),
        )
        body = json.dumps(_pr_payload("opened")).encode()
        resp = client.post(
            "/webhooks/github", content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": _sign(body, "test-secret"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0][0] == 479
        assert calls[0][1] == "head1234"


class TestEventDispatch:
    def _post(self, payload: dict, event: str = "pull_request") -> "httpx.Response":  # noqa: F821
        body = json.dumps(payload).encode()
        return client.post(
            "/webhooks/github", content=body,
            headers={
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": _sign(body, "test-secret"),
                "Content-Type": "application/json",
            },
        )

    def test_synchronize_dispatches_a_job(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            JobRegistry, "start",
            lambda self, pr, head_sha, cs_id, coro_factory: calls.append(cs_id),
        )
        resp = self._post(_pr_payload("synchronize"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        assert len(calls) == 1

    def test_unrelated_action_is_ignored(self, monkeypatch):
        calls = []
        monkeypatch.setattr(JobRegistry, "start", lambda self, *a, **k: calls.append(a))
        resp = self._post(_pr_payload("labeled"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert calls == []

    def test_closed_without_merge_is_ignored(self):
        resp = self._post(_pr_payload("closed", merged=False))
        assert resp.status_code == 200
        assert resp.json()["reason"] == "closed without merging"

    async def test_closed_and_merged_with_no_prior_changeset_is_skipped(self, falkordb_or_skip):
        resp = self._post(_pr_payload("closed", pr_number=999999, merged=True))
        assert resp.status_code == 200
        assert resp.json()["merge_flow"] == "skipped (no prior ChangeSet)"

    def test_push_event_is_acknowledged_not_processed(self):
        resp = self._post({"ref": "refs/heads/main"}, event="push")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_unknown_event_is_ignored(self):
        resp = self._post({"anything": True}, event="star")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "event": "star"}
