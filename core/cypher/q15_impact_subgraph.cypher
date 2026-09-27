// Q15 — impact subgraph for the report UI (ChangeSet -> Answer <- Question,
// plus the documents each answer's evidence touches).
// Params: $cs_id
MATCH (cs:ChangeSet {id: $cs_id})-[i:IMPACTS]->(a:Answer)<-[:ANSWERED_BY]-(q:Question)
OPTIONAL MATCH (a)-[:USED_CHUNK]->(:Chunk)<-[:PART_OF]-(d:Document)
RETURN q.text AS question, a.text AS old_answer, i.new_answer AS new_answer,
       i.tier AS tier, i.verdict AS verdict, i.via AS via, collect(DISTINCT d.id) AS docs
