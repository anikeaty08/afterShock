// Q6 — T4 Reverse retrieval: changed fact -> nearest stored questions.
// FalkorDB's vector index returns a cosine *distance* — lower is closer.
// Convert to similarity as (1 - score) before comparing to theta4 in Python.
// Params: $fact_embedding (vecf32-compatible list[float]), $k (int, e.g. 10)
CALL db.idx.vector.queryNodes('Question', 'embedding', $k, vecf32($fact_embedding))
YIELD node, score
MATCH (node)-[:ANSWERED_BY]->(a:Answer {status: 'current'})
RETURN a.id AS answer_id, node.text AS question, score, 'T4' AS tier
