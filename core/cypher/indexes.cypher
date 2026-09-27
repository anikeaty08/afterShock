// Indexes created once at bootstrap post-ingest (§6.3). Each statement is
// idempotent-by-catch: core/ledger.py::ensure_indexes() passes
// expected_errors=("already indexed",) so a re-run is a no-op, not a crash.
//
// The vector index's OPTIONS map takes its dimension as a literal, matching
// every example on FalkorDB's own vector-index docs page (params inside a
// map literal in a DDL-style clause aren't part of the documented syntax) —
// core/ledger.py::ensure_indexes() renders {dimensions} via str.format()
// from Settings.embed_dimensions before sending the statement. That value
// is trusted server-side config, never user input, so the interpolation
// carries no injection risk.
CREATE INDEX FOR (q:Question) ON (q.id)
;
CREATE INDEX FOR (a:Answer) ON (a.id)
;
CREATE INDEX FOR (a:Answer) ON (a.status)
;
CREATE INDEX FOR (cs:ChangeSet) ON (cs.id)
;
CREATE VECTOR INDEX FOR (q:Question) ON (q.embedding) OPTIONS {{dimension: {dimensions}, similarityFunction: 'cosine'}}
