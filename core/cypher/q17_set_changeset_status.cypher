// Q17 — incremental ChangeSet status write, for SSE progress
// (queued -> running -> done|failed). q12a overwrites status/timings again
// at the end with the final report; this is the interim heartbeat.
// Params: $cs_id, $pr, $head, $status, $stage
MERGE (cs:ChangeSet {id: $cs_id})
SET cs.pr = $pr, cs.head_sha = $head, cs.status = $status, cs.stage = $stage,
    cs.updated_at = timestamp()
