# Aftershock — MCP server (§10.3, stretch)
#
# Not a fresh implementation of the product's logic: ``ask_docs`` and
# ``preview_impact`` are thin wrappers over the REST API (POST /ask,
# POST /impact/preview) — the same distinction the design doc draws between
# this and FalkorDB's own official MCP server (raw graph access): that one
# is for the database, this one is for the *product*, so a caller never
# needs to know Cypher or the graph model to ask a question or check a docs
# edit for blast radius.
#
# Run: `python -m mcp_server.server` (stdio transport), with the Aftershock
# API already running at AFTERSHOCK_API_URL (default http://localhost:8000).

from __future__ import annotations

import os
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

API_BASE = os.environ.get("AFTERSHOCK_API_URL", "http://localhost:8000")

mcp = MCPServer(
    name="aftershock",
    instructions=(
        "Aftershock answers questions over a docs repo and can preview which "
        "of those answers a docs edit would break, before a PR exists. "
        "Use ask_docs for a question about the docs. Use preview_impact "
        "before committing an edit, to see which existing answers it changes."
    ),
)


class PreviewFile(BaseModel):
    path: str = Field(description="Repo-relative path, e.g. 'graphrag/getting-started.mdx'")
    content: str = Field(description="The file's full proposed content")
    op: Literal["added", "modified", "deleted"]


@mcp.tool(description="Ask a question over the docs corpus. Returns the answer and its evidence (documents, facts) from the knowledge graph, or an explicit abstention if the graph has no supporting evidence.")
async def ask_docs(question: str) -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=120.0) as client:
        resp = await client.post("/ask", json={"question": question})
        resp.raise_for_status()
        return resp.json()


@mcp.tool(description="Preview which existing answers a proposed docs edit would break, before any PR exists. Pass the full new/changed/deleted file contents; this runs the same impact analysis Aftershock runs on a real PR, without persisting anything.")
async def preview_impact(files: list[PreviewFile]) -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=300.0) as client:
        resp = await client.post(
            "/impact/preview",
            json={"files": [f.model_dump() for f in files]},
        )
        resp.raise_for_status()
        return resp.json()


def main() -> None:
    import asyncio

    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
