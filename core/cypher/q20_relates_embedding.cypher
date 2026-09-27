// Q20 — a specific RELATES edge's stored embedding, by its endpoints + type.
// Companion to q19: lets impact.py's T3 gate and the T4 reverse-retrieval
// query vector (Q6) reuse graphrag_sdk's own relationship embeddings instead
// of re-embedding fact text.
// Params: $src, $rel, $tgt (all __Entity__.id / rel_type — ids, not names)
MATCH (s:__Entity__ {id: $src})-[r:RELATES {rel_type: $rel}]->(t:__Entity__ {id: $tgt})
RETURN r.embedding AS embedding
LIMIT 1
