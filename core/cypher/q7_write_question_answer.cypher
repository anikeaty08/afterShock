// Q7 — write a Question + Answer, linked by ANSWERED_BY.
// Params: $qid, $qtext, $qemb (list[float]), $source, $aid, $answer, $sha, $graph,
//         $abstained (bool), $fact_keys, $entity_ids, $doc_ids (lists of str), $model
MERGE (q:Question {id: $qid})
  ON CREATE SET q.text = $qtext, q.embedding = vecf32($qemb), q.source = $source, q.created_at = timestamp()
CREATE (a:Answer {id: $aid, text: $answer, commit_sha: $sha, graph: $graph, status: 'current',
                  abstained: $abstained, fact_keys: $fact_keys, entity_ids: $entity_ids,
                  doc_ids: $doc_ids, model: $model, created_at: timestamp()})
CREATE (q)-[:ANSWERED_BY]->(a)
