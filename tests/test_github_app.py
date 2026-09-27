# Tests for api/github_app.py. No live GitHub App credentials are available
# (see AI_USAGE.md), so HMAC verification and JWT claims are tested as pure
# functions, and the REST calls are tested against httpx.MockTransport
# (httpx's own supported no-network testing mechanism) rather than skipped.

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import jwt
import pytest

from api.github_app import GitHubClient, GitHubContext, make_app_jwt, verify_webhook_signature
from core.config import Settings
from core.ingest import PRFlowReport

@pytest.fixture
def rsa_keypair():
    """A real, freshly generated RSA keypair — pyjwt's RS256 needs an
    actual valid key, not a placeholder string, to sign/verify against."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


class TestVerifyWebhookSignature:
    def test_valid_signature_passes(self):
        secret = "s3cret"
        payload = b'{"action": "opened"}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(payload, sig, secret) is True

    def test_wrong_secret_fails(self):
        payload = b'{"action": "opened"}'
        sig = "sha256=" + hmac.new(b"wrong", payload, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(payload, sig, "s3cret") is False

    def test_tampered_payload_fails(self):
        secret = "s3cret"
        sig = "sha256=" + hmac.new(secret.encode(), b'{"action": "opened"}', hashlib.sha256).hexdigest()
        assert verify_webhook_signature(b'{"action": "closed"}', sig, secret) is False

    def test_missing_header_fails(self):
        assert verify_webhook_signature(b"x", None, "s3cret") is False

    def test_malformed_header_fails(self):
        assert verify_webhook_signature(b"x", "not-sha256=abc", "s3cret") is False


class TestMakeAppJwt:
    def test_claims_are_well_formed_and_verifiable(self, rsa_keypair):
        private_pem, public_pem = rsa_keypair
        token = make_app_jwt("12345", private_pem)
        decoded = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_exp": False})
        assert decoded["iss"] == "12345"
        now = int(time.time())
        assert decoded["iat"] <= now - 30  # backdated for clock skew
        assert decoded["exp"] - decoded["iat"] <= 10 * 60  # under GitHub's 10 min cap


def _make_report(coverage_regression: bool) -> PRFlowReport:
    from core.ingest import ImpactReportEntry

    entries = []
    if coverage_regression:
        entries.append(
            ImpactReportEntry(
                answer_id="a1", question="q", tier="T1", via=["docs/x.mdx"],
                verdict="NOW_ABSTAINS", old_answer="Yes.", new_answer="I don't know.",
                reason="evidence deleted", score=None,
            )
        )
    return PRFlowReport(
        changeset_id="pr-1-abc1234", pr=1, base_sha="base", head_sha="abc1234",
        doc_ids=["docs/x.mdx"], facts_added=0, facts_removed=1, facts_modified=0,
        tier_raw_counts={"T1": 1, "T2": 0, "T3": 0, "T4": 0}, truncated=False,
        entries=entries, apply_result=None, timings_ms={"total": 100},
    )


class TestGitHubClientAgainstMockTransport:
    def _client(self, handler) -> GitHubClient:
        settings = Settings(github_app_id="123", github_private_key="dummy")
        client = GitHubClient(settings)
        client._installation_tokens[999] = ("fake-installation-token", time.time() + 3600)

        async def _request(method, installation_id, path, **kwargs):
            transport = httpx.MockTransport(handler)
            headers = {"Authorization": "token fake-installation-token", **kwargs.pop("headers", {})}
            async with httpx.AsyncClient(transport=transport, base_url=settings.github_api_base) as c:
                resp = await c.request(method, path, headers=headers, **kwargs)
                resp.raise_for_status()
                return resp

        client._request = _request  # bypass the real installation-token exchange entirely
        return client

    async def test_upsert_creates_new_comment_when_none_exists(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, str(request.url)))
            if request.method == "GET":
                return httpx.Response(200, json=[])
            body = json.loads(request.content)
            assert "<!-- aftershock -->" in body["body"]
            return httpx.Response(201, json={"id": 42})

        client = self._client(handler)
        ctx = GitHubContext(owner="FalkorDB", repo="docs", pr_number=479, installation_id=999, head_sha="abc", base_sha="def")
        comment_id = await client.upsert_pr_comment(ctx, "### Report")
        assert comment_id == 42
        assert calls[0][0] == "GET"
        assert calls[1][0] == "POST"

    async def test_upsert_patches_existing_marked_comment(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 7, "body": "<!-- aftershock -->\nold report"}])
            if request.method == "PATCH":
                assert request.url.path.endswith("/issues/comments/7")
                return httpx.Response(200, json={"id": 7})
            raise AssertionError(f"unexpected {request.method}")

        client = self._client(handler)
        ctx = GitHubContext(owner="FalkorDB", repo="docs", pr_number=479, installation_id=999, head_sha="abc", base_sha="def")
        comment_id = await client.upsert_pr_comment(ctx, "### New report")
        assert comment_id == 7

    async def test_check_run_conclusion_is_neutral_on_coverage_regression(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 1})

        client = self._client(handler)
        ctx = GitHubContext(owner="FalkorDB", repo="docs", pr_number=479, installation_id=999, head_sha="abc", base_sha="def")
        await client.create_check_run(ctx, _make_report(coverage_regression=True))
        assert captured["body"]["conclusion"] == "neutral"

    async def test_check_run_conclusion_is_success_without_regression(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 1})

        client = self._client(handler)
        ctx = GitHubContext(owner="FalkorDB", repo="docs", pr_number=479, installation_id=999, head_sha="abc", base_sha="def")
        await client.create_check_run(ctx, _make_report(coverage_regression=False))
        assert captured["body"]["conclusion"] == "success"
