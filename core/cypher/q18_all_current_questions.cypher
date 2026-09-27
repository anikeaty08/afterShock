// Q18 — every currently-answered question, for the Replay Lab oracle
// (§7.5 step 2: re-answer *all* questions at each graph state) and for
// bootstrap's initial answer-the-question-bank pass.
// No params.
MATCH (q:Question)-[:ANSWERED_BY]->(a:Answer {status: 'current'})
RETURN q.id AS question_id, q.text AS question_text, q.source AS source
