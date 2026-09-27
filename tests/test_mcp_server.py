# Tests for mcp_server/server.py — thin-wrapper forwarding, no real API or
# LLM needed (httpx.MockTransport stands in for the Aftershock API).

from __future__ import annotations

import json

import httpx
import pytest

import mcp_server.server as server_mod
from mcp_server.server import PreviewFile, ask_docs, preview_impact


_RealAsyncClient = httpx.AsyncClient
"""Captured before any monkeypatching — httpx is one shared module object,
so patching server_mod.httpx.AsyncClient also changes what `httpx.AsyncClient`
resolves to right here; using the module attribute inside __call__ below
would just call the patch recursively."""


class _PatchedAsyncClient:
    """Drop-in for httpx.AsyncClient that routes through a MockTransport
    regardless of the base_url/timeout the tool functions pass."""

    def __init__(self, handler):
        self._handler = handler

    def __call__(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(self._handler)
        return _RealAsyncClient(*args, **kwargs)


@pytest.fixture
def patch_httpx(monkeypatch):
    def _apply(handler):
        monkeypatch.setattr(server_mod.httpx, "AsyncClient", _PatchedAsyncClient(handler))

    return _apply


async def test_ask_docs_forwards_question_and_returns_json(patch_httpx):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(200, json={"answer": "Yes.", "abstained": False})

    patch_httpx(handler)
    result = await ask_docs("Does FalkorDB support GRAPH.COPY?")
    assert captured["path"] == "/ask"
    assert captured["body"] == {"question": "Does FalkorDB support GRAPH.COPY?"}
    assert result == {"answer": "Yes.", "abstained": False}


async def test_preview_impact_forwards_files(patch_httpx):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"impacted": []})

    patch_httpx(handler)
    files = [PreviewFile(path="docs/x.mdx", content="new text", op="modified")]
    result = await preview_impact(files)
    assert captured["body"] == {"files": [{"path": "docs/x.mdx", "content": "new text", "op": "modified"}]}
    assert result == {"impacted": []}
