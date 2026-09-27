// Q12 — persist a ChangeSet's report: the node itself, TOUCHES edges to
// affected documents, and IMPACTS edges to every flagged Answer.
// Run as two statements (TOUCHES needs doc ops, IMPACTS needs the impact list) —
// see core/ledger.py::persist_changeset(), which issues q12a then q12b then q12c.

// q12a — upsert the ChangeSet node itself.
// Params: $cs_id, $pr, $base, $head, $doc_ids, $status, $timings_json
MERGE (cs:ChangeSet {id: $cs_id})
SET cs.pr = $pr, cs.base_sha = $base, cs.head_sha = $head, cs.doc_ids = $doc_ids,
    cs.status = $status, cs.timings = $timings_json, cs.created_at = timestamp()
