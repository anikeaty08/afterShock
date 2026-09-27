// Q12c — IMPACTS edges from a ChangeSet to every re-checked Answer.
// Params: $cs_id, $impacts — list of {answer_id, tier, via, verdict, new_answer, reason, score}
MATCH (cs:ChangeSet {id: $cs_id})
UNWIND $impacts AS imp
MATCH (a:Answer {id: imp.answer_id})
MERGE (cs)-[i:IMPACTS]->(a)
SET i.tier = imp.tier, i.via = imp.via, i.verdict = imp.verdict,
    i.new_answer = imp.new_answer, i.judge_reason = imp.reason, i.score = imp.score
