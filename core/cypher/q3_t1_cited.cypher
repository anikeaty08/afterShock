// Q3 — T1 Cited: answers that used a chunk of a changed document.
// Params: $doc_ids (list of Document.id)
MATCH (d:Document)-[:PART_OF]->(:Chunk)<-[:USED_CHUNK]-(a:Answer {status: 'current'})
WHERE d.id IN $doc_ids
RETURN a.id AS answer_id, collect(DISTINCT d.id) AS via, 'T1' AS tier
