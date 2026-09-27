// Q10 — resolve a "src -[TYPE]-> tgt: fact" line back to its fact_key.
// Params: $src (entity name), $rel (rel_type), $tgt (entity name)
MATCH (s:__Entity__ {name: $src})-[r:RELATES {rel_type: $rel}]->(t:__Entity__ {name: $tgt})
RETURN s.id + '|' + r.rel_type + '|' + t.id AS fact_key, s.id AS src_id, t.id AS tgt_id
LIMIT 1
