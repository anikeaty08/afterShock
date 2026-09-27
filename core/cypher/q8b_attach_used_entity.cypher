// Q8b — attach USED_ENTITY evidence edges for one answer.
// Params: $aid, $entity_ids (list[str])
MATCH (a:Answer {id: $aid})
UNWIND $entity_ids AS eid
MATCH (e:__Entity__ {id: eid})
CREATE (a)-[:USED_ENTITY]->(e)
