// Q9 — resolve a retrieved passage back to its source chunk.
// Params: $doc_id, $passage_prefix (first ~120 normalised chars of the passage)
MATCH (d:Document {id: $doc_id})-[:PART_OF]->(c:Chunk)
WHERE c.text CONTAINS $passage_prefix
RETURN c.id AS chunk_id
LIMIT 1
