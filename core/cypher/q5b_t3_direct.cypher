// Q5b — T3 1-hop variant: answers that used the changed entity itself
// (as opposed to a neighbour of it). Companion to q5_t3_neighbour.cypher.
// Params: $changed_entity_ids, $already_flagged
MATCH (x:__Entity__)<-[:USED_ENTITY]-(a:Answer {status: 'current'})
WHERE x.id IN $changed_entity_ids AND NOT a.id IN $already_flagged
RETURN a.id AS answer_id, collect(DISTINCT x.name) AS via, 'T3' AS tier
