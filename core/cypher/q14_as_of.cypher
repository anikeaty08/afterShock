// Q14 — "as of" ledger time-travel view for one question.
// Params: $qid, $as_of (epoch millis, matching timestamp())
MATCH (:Question {id: $qid})-[:ANSWERED_BY]->(a:Answer)
WHERE a.created_at <= $as_of
RETURN a.text, a.commit_sha, a.created_at
ORDER BY a.created_at DESC
LIMIT 1
