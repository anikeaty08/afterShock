// Q12b — TOUCHES edges from a ChangeSet to the documents it added/modified/deleted.
// Params: $cs_id, $touches — list of {doc_id, op}
MATCH (cs:ChangeSet {id: $cs_id})
UNWIND $touches AS t
MERGE (d:Document {id: t.doc_id})
MERGE (cs)-[e:TOUCHES]->(d)
SET e.op = t.op
