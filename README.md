# Aftershock

**See what a docs change breaks *before* it merges.**

Full product design: [`aftershock-system-design.md`](aftershock-system-design.md).

Aftershock answers questions over a docs repo with the [FalkorDB GraphRAG
SDK](https://github.com/FalkorDB/GraphRAG-SDK) and writes every answer into
FalkorDB as an `Answer` node, linked to the chunks, entities and facts it
relied on. On a docs PR, it copies the graph (`GRAPH.COPY`), applies the PR
with `apply_changes()`, diffs the facts, and runs a multi-hop impact
traversal to find every answer the PR can affect — including answers that
never cited the changed page. FalkorDB is the only database.

This repo currently holds the **backend** (`core/`, `api/`, `mcp_server/`,
`infra/`) — the graph model, the Cypher catalog, and the pipelines in §7 of
the design doc. `web/` (the report/ledger/ask/lab frontend) isn't built yet.

## Status

| Piece | State |
|---|---|
| `core/` — graph access, ledger, evidence resolver, fact diff, impact tiers T1–T4, abstention-gated re-answer, two-stage judge, bootstrap/PR-flow/merge-flow orchestration | **Built, tested against a live FalkorDB** (PR flow and merge flow proven end-to-end in `tests/test_ingest.py` / `tests/test_merge_flow.py`, LLM boundary faked — no key in this environment) |
| `api/` — FastAPI app (`/ask`, `/webhooks/github`, `/changesets/*`, `/impact/preview`, `/health`), GitHub App client, job runner | **Built and tested** (signature verification, event dispatch, changeset reads all exercised; `/ask` and `/impact/preview`'s pipeline is the same one `tests/test_ingest.py` covers end-to-end — no separate live-LLM test) |
| `mcp_server/` — `ask_docs` / `preview_impact` MCP tools | **Built** (thin wrappers over the REST API above) |
| `web/` — report page, ledger, ask, Replay Lab dashboard | Not yet built |
| `eval/` — Replay Lab | Not yet built (`GET /replay/summary` says so rather than faking numbers) |

## Setup

### 1. FalkorDB (via Docker)

```bash
docker compose -f infra/docker-compose.yml up -d
```

> **If you're on Windows with WSL2 installed**, FalkorDB is published on host
> port **16379**, not the usual 6379. WSL2's own localhost relay
> (`wslrelay.exe`) can already own `127.0.0.1:6379` / `[::1]:6379` for
> whatever's running inside your WSL distro, silently shadowing Docker's
> forwarded port for anything that resolves `localhost` to a loopback address
> before the IPv6-wildcard bind Docker actually owns. Symptom if this ever
> looks wrong on your machine: queries fail with `unknown command
> 'GRAPH.QUERY'` because a plain Redis inside WSL answered instead of this
> container. See the comment in `infra/docker-compose.yml`.

FalkorDB Browser: http://localhost:3100 (moved off its usual :3000 so it
doesn't collide with a Next.js dev server later).

### 2. Python environment

```bash
uv sync --extra dev        # creates .venv, installs core/ + dev deps
cp .env.example .env       # fill in FALKORDB_PORT (16379 per above) and an LLM key
```

Needs Python ≥3.10 (developed against 3.12). `graphrag-sdk[litellm,markdown]`
is the SDK dependency; `falkordb` is used directly for `GRAPH.COPY` /
`GRAPH.LIST` (§7.3), which the SDK itself doesn't wrap.

### 3. Run the tests

```bash
.venv/Scripts/pytest tests/ -v      # Windows
.venv/bin/pytest tests/ -v          # macOS/Linux
```

Unit tests (evidence-string parsing, judge stage 1, the abstention gate)
need nothing but Python. Integration tests need FalkorDB running — they
skip cleanly (not fail) if it isn't reachable, via the `falkordb_or_skip`
fixture in `tests/conftest.py`. None of the currently-built tests need an
LLM API key: the judge and re-answer tests fake out the LLM boundary, since
graphrag_sdk's own `retrieve()`/`completion()`/`CosineReranker` are its
tested responsibility, not ours to re-verify.

## Running the API

```bash
.venv/Scripts/uvicorn api.main:app --reload   # Windows
.venv/bin/uvicorn api.main:app --reload       # macOS/Linux
```

Endpoints: `POST /ask`, `POST /webhooks/github`, `GET /changesets/{id}`
(+ `/graph`, `/events` SSE), `POST /impact/preview`, `GET /replay/summary`,
`GET /health`. `/ask` and `/impact/preview` need `docs_main` bootstrapped
and an LLM key in `.env` — neither has happened yet in this environment
(see the status table above).

### Aftershock's own MCP server (`mcp_server/`, §10.3)

```bash
AFTERSHOCK_API_URL=http://localhost:8000 python -m mcp_server.server
```

Exposes `ask_docs(question)` and `preview_impact(files)` as MCP tools —
thin wrappers over the two endpoints above, for an MCP client (Claude Code,
Cursor, ...) to check "what does my docs edit break?" before a PR exists.
This is distinct from the FalkorDB MCP server below: that one is raw graph
access, this one is the product.

## FalkorDB MCP server (raw graph access)

The official [`@falkordb/mcpserver`](https://www.npmjs.com/package/@falkordb/mcpserver)
is registered for this project (`claude mcp list`) against the FalkorDB
container above, for ad hoc `GRAPH.QUERY` / `GRAPH.RO_QUERY` exploration from
an MCP-aware client. It's a generic graph tool, not part of the product —
`mcp_server/` (not yet built) will be Aftershock's own `ask_docs` /
`preview_impact` tools (§10.3), thin wrappers over the REST API rather than
raw Cypher access.

## Graph model

See design doc §6. Two layers in one graph:

- **SDK layer** (built by GraphRAG SDK, read-only for us): `Document
  -[:PART_OF]-> Chunk -[:NEXT_CHUNK]-> Chunk`, `__Entity__
  -[:MENTIONED_IN]-> Chunk`, `__Entity__ -[:RELATES {rel_type, fact,
  embedding}]-> __Entity__`. Entity ids are deterministic (`name__type`,
  via `compute_entity_id`), which is what makes a fact diff stable across a
  `GRAPH.COPY`'d graph.
- **Aftershock layer** (ours): `Question`, `Answer`, `ChangeSet` nodes;
  `ANSWERED_BY`, `USED_CHUNK`, `USED_ENTITY`, `SUPERSEDES`, `TOUCHES`,
  `IMPACTS` edges.

One deliberate deviation from the design doc's original sketch: `q2_scope_facts.cypher`
also returns `r.embedding`. `finalize()` already embeds every `RELATES` edge
in the same space as `Question.embedding`, so the fact-diff's re-extraction-
noise filter and the T3/T4 impact tiers reuse that stored embedding instead
of re-embedding fact text — same correctness, fewer LLM calls.

## Cypher catalog

Every query the product depends on lives in [`core/cypher/`](core/cypher/),
one file per named query (`q1_scope_entities.cypher`, `q6_t4_reverse_retrieval.cypher`,
...), loaded by name via `core.graph.cypher_text()` / `GraphHandle.run_named()`.
Each file's header comment says which design-doc query it implements and
what its params are. `core/impact.py` and `core/diff.py` also each have two
or three small utility queries (`q11b`, `q19`, `q20`, ...) added beyond the
original §8 numbering — documented inline at each call site, not silently.

Verified against FalkorDB's actual Cypher support (not assumed from Neo4j
familiarity) — two syntax corrections came out of building this:

- Vector index creation is `CREATE VECTOR INDEX FOR (n:Label) ON (n.attr)
  OPTIONS {dimension: N, similarityFunction: 'cosine'}` — **not**
  `db.idx.vector.createNodeIndex(...)` (that's Neo4j's procedure name).
- `db.idx.vector.queryNodes(...)` returns a cosine **distance** (lower is
  closer); `graphrag_sdk`'s own `vector_store.py` converts with `1 - score`
  before treating it as a similarity, and `core/impact.py`'s T4 tier does
  the same.

## Known SDK-version gotcha

`core/` was built against `graphrag-sdk==1.4.0` (the published PyPI
release). Its GitHub `main` branch is slightly ahead in a couple of places
that looked like current API while reading the source for reference, but
raised `TypeError: unexpected keyword argument` at runtime against the
installed release:

- `FalkorDBConnection.query()` on `main` takes an `expected_errors=` kwarg
  that 1.4.0 doesn't have — `core/ledger.py`'s `ensure_indexes()` now
  matches "already indexed" by message text instead.
- `GraphRAG.__init__()` on `main` takes an `enable_cypher=` kwarg (the
  text-to-Cypher retrieval path) that 1.4.0 doesn't have —
  `core/ingest.py::build_rag()` leaves it out for now; worth adding back
  once the SDK release that has it ships (see its docstring).

If you see an `unexpected keyword argument` error from a `graphrag_sdk`
call, it's worth diffing the installed package against `main` before
assuming the call itself is wrong.

## AI usage

See [`AI_USAGE.md`](AI_USAGE.md).
