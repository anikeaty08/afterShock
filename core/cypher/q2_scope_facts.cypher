// Q2 — all facts touching the scope (run on both graphs; diff by fact key in Python).
// Params: $scope (list of __Entity__.id)
//
// Deviation from the original catalog sketch in SYSTEM_DESIGN.md §8: also
// returns r.embedding. graphrag_sdk's finalize() already embeds every
// RELATES edge (FinalizeResult.relationships_embedded) in the same
// embedding space as Question.embedding — reusing it here means diff.py's
// "same key, different text" check (and impact.py's T3/T4 semantic gates)
// need zero extra LLM embedding calls. Falls back to re-embedding via an
// injected embedder only when a row's embedding is missing (e.g. an older
// graph ingested before this field existed).
MATCH (s:__Entity__)-[r:RELATES]->(t:__Entity__)
WHERE s.id IN $scope OR t.id IN $scope
RETURN s.id AS src, r.rel_type AS rel, t.id AS tgt, r.fact AS fact, r.embedding AS embedding
