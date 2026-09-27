// Q21 — a ChangeSet's IMPACTS edges with a real verdict (not UNCHANGED), for
// the merge flow (§7.4 step 2): each of these gets re-answered on docs_main
// and SUPERSEDEs its old Answer.
// Params: $cs_id
MATCH (cs:ChangeSet {id: $cs_id})-[i:IMPACTS]->(a:Answer)<-[:ANSWERED_BY]-(q:Question)
WHERE i.verdict <> 'UNCHANGED'
RETURN a.id AS old_answer_id, q.id AS question_id, q.text AS question_text, i.verdict AS verdict
