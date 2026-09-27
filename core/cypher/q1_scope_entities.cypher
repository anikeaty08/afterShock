// Q1 — entities mentioned in changed documents (run on both graphs: old side and new side).
// Params: $doc_ids (list of Document.id, repo-relative paths)
MATCH (d:Document)-[:PART_OF]->(:Chunk)<-[:MENTIONED_IN]-(e:__Entity__)
WHERE d.id IN $doc_ids
RETURN DISTINCT e.id AS entity_id
