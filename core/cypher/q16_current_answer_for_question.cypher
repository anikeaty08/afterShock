// Q16 — current answer + its question text, for iterating an IMPACTS set
// during re-answer (§7.3 step 7). Not in the original §8 numbering; added
// alongside it because the PR flow needs it to drive the re-answer loop.
// Params: $answer_id
MATCH (q:Question)-[:ANSWERED_BY]->(a:Answer {id: $answer_id, status: 'current'})
RETURN q.id AS question_id, q.text AS question_text, a.text AS old_answer,
       a.abstained AS old_abstained, a.fact_keys AS fact_keys,
       a.entity_ids AS entity_ids, a.doc_ids AS doc_ids
LIMIT 1
