// Q19 — a question's stored embedding, given the id of its current answer.
// Used by impact.py's T3 semantic gate (theta3): compares this against a
// changed fact's stored RELATES.embedding, both already in the same
// embedding space from ingestion — no LLM call needed at impact-analysis time.
// Params: $answer_id
MATCH (q:Question)-[:ANSWERED_BY]->(a:Answer {id: $answer_id})
RETURN q.embedding AS embedding
LIMIT 1
