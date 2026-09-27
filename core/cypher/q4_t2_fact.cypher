// Q4 — T2 Fact: answers whose context contained a fact key the PR changed.
// Params: $changed_fact_keys (list of "src|rel|tgt" strings)
MATCH (a:Answer {status: 'current'})
WHERE any(k IN a.fact_keys WHERE k IN $changed_fact_keys)
RETURN a.id AS answer_id,
       [k IN a.fact_keys WHERE k IN $changed_fact_keys] AS via,
       'T2' AS tier
