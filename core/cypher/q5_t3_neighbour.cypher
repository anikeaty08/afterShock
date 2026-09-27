// Q5 — T3 Neighbour: changed entity -> neighbour -> answer (hub-dampened).
// The semantic gate (theta3) runs in Python on these candidates, not here.
// Params: $changed_entity_ids, $hub_ids, $already_flagged (all lists of ids/strings)
MATCH (x:__Entity__)-[:RELATES]-(y:__Entity__)
WHERE x.id IN $changed_entity_ids AND NOT y.id IN $hub_ids
MATCH (y)<-[:USED_ENTITY]-(a:Answer {status: 'current'})
WHERE NOT a.id IN $already_flagged
RETURN a.id AS answer_id, collect(DISTINCT x.name + ' -> ' + y.name) AS via, 'T3' AS tier
