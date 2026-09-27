// Q13 — supersede an answer on merge (§7.4 step 2).
// Params: $old_id, $new_id, $pr, $verdict
MATCH (old:Answer {id: $old_id}), (new:Answer {id: $new_id})
SET old.status = 'superseded'
CREATE (new)-[:SUPERSEDES {pr: $pr, verdict: $verdict}]->(old)
