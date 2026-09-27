// Q11b — raw per-entity RELATES degree, used in Python to compute the
// hub_threshold (95th percentile) that q11_hub_entities.cypher then applies.
// No params.
MATCH (e:__Entity__)-[r:RELATES]-()
RETURN e.id AS entity_id, count(r) AS degree
