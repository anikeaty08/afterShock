// Q8a — attach USED_CHUNK evidence edges for one answer.
// Params: $aid, $chunk_ids (list[str])
MATCH (a:Answer {id: $aid})
UNWIND $chunk_ids AS cid
MATCH (c:Chunk {id: cid})
CREATE (a)-[:USED_CHUNK]->(c)
