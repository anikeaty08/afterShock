# AI assistant disclosure

Per the hackathon's rule 10 (see `aftershock-system-design.md` §14).

**Assistant:** Claude (Anthropic), via Claude Code, in an agentic
pair-programming session — reading the design doc, exploring the real
`graphrag-sdk` and FalkorDB documentation/source, writing code, running it
against a live local FalkorDB instance, and iterating on failures.

**What that looked like in practice, for this backend build:**

- The design doc (`aftershock-system-design.md`) was written before this
  session and treated as the spec.
- Before writing any `core/` code, the assistant cloned `FalkorDB/GraphRAG-SDK`
  and `FalkorDB/docs` locally (both public) to read the actual installed SDK
  source and real docs pages, rather than generating code from memory of the
  API. This caught several real discrepancies before they became bugs:
  - The design doc's ontology sketch used `GraphSchema`/`EntityType`/
    `RelationType` — these are deprecated aliases as of SDK v1.2+; the
    current names (`Ontology`/`Entity`/`Relation`) are what `core/ontology/schema.json`
    and the codebase actually use.
  - `db.idx.vector.createNodeIndex(...)` (assumed by analogy to other graph
    databases) doesn't exist in FalkorDB; the real syntax is `CREATE VECTOR
    INDEX FOR (n:Label) ON (n.attr) OPTIONS {...}`.
  - The GitHub `main` branch of `graphrag-sdk` is slightly ahead of the
    published PyPI release (1.4.0) actually installed — an `expected_errors=`
    kwarg on `FalkorDBConnection.query()` and an `enable_cypher=` kwarg on
    `GraphRAG.__init__()` both exist on `main` but not in 1.4.0, and code
    written against the cloned `main` source failed at runtime (a
    `TypeError: unexpected keyword argument`) until corrected to match the
    installed version, twice, in two different modules.
  - A local port conflict was found and fixed: on this development machine,
    WSL2's own localhost-forwarding relay already occupies
    `127.0.0.1:6379`/`[::1]:6379` (serving a plain Redis inside the WSL
    distro), which silently shadowed FalkorDB's Docker container on the same
    port. FalkorDB is now published on host port 16379 instead
    (`infra/docker-compose.yml`), documented with the failure symptom
    (`unknown command 'GRAPH.QUERY'`) so it's diagnosable if it recurs on
    another machine.
  - A clock-skew bug was found in `core/ledger.py`'s `as_of()`: computing
    "now" from the Python process's own clock and comparing it against a
    `created_at` stamped by FalkorDB's `timestamp()` (running inside a WSL2
    VM, whose clock can drift from the host by hundreds of ms) could hide a
    just-written row. Fixed by asking FalkorDB for its own `timestamp()`
    instead of the host clock, so the comparison is server-time-to-server-time.
- Every module in `core/` was exercised against a real, locally running
  FalkorDB container as it was written — not just imported and assumed
  correct. `tests/` includes both fast unit tests (string parsing, judge
  stage-1 logic) and integration tests that run real Cypher against a
  throwaway graph per test (`tests/conftest.py`), covering the fact diff and
  all four impact tiers (T1–T4, including the hub-dampening and
  semantic-gate filters actually filtering something in the test data, and a
  real vector-index reverse-retrieval query for T4).
- One deliberate, documented deviation from the design doc's own Cypher
  sketch: `q2_scope_facts.cypher` also returns the `RELATES` edge's stored
  embedding (which `finalize()` already computes), so the fact-diff's
  rephrasing filter and the T3/T4 semantic gates need zero additional LLM
  embedding calls. Noted in the file's own header comment and in the README,
  not silently introduced.
- No LLM API key was available in the build environment, so the
  LLM-dependent paths (`core/reanswer.py`'s generation call,
  `core/judge.py`'s stage-2 LLM judge) are implemented against the SDK's
  documented/verified interfaces and unit-tested with a faked LLM boundary,
  but have not yet been run end-to-end against a real model. This is called
  out explicitly rather than left implicit.

**What a human should still check:** the actual judgment quality of the
stage-2 LLM judge and the calibration of `min_evidence_score`/`theta3`/
`theta4` (§7.5 says these need measuring on the real corpus, not guessing —
that hasn't happened yet, since it needs a live LLM key and the bootstrap
corpus ingested).
