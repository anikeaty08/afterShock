// Q11 — hub entities for T3 dampening. Threshold = 95th percentile RELATES
// degree, computed at bootstrap and recomputed after each merge (§7.4).
// Params: $hub_threshold (int)
MATCH (e:__Entity__)-[r:RELATES]-()
WITH e, count(r) AS degree
WHERE degree > $hub_threshold
RETURN e.id AS entity_id, degree
