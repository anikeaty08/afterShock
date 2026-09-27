# Integration test for core/impact.py's T1-T4 tiers against a live FalkorDB.
# Covers, in one scenario: T1 citation, T2 fact-key match, T3 with both the
# hub-dampening and semantic-gate (theta3) filters actually filtering
# something, and T4's vector-index reverse retrieval — the whole point of
# §4.2's "why a vector store can't do it" table.

from __future__ import annotations

from core.config import Settings
from core.diff import FactDiff, FactRow
from core.graph import GraphHandle
from core.impact import compute_impact
from core.ledger import Ledger

DOC = "docs/api.mdx"


async def _build_graph(g: GraphHandle) -> None:
    await g.run(
        """
        CREATE (d:Document {id: $doc})
        CREATE (c:Chunk {id:'chunk-api', text:'api chunk'})
        CREATE (d)-[:PART_OF]->(c)
        CREATE (sdk:__Entity__ {id:'graphrag_sdk__product', name:'GraphRAG SDK'})
        CREATE (newc:__Entity__ {id:'newclass__apiclass', name:'GraphRAG'})
        CREATE (oldc:__Entity__ {id:'oldclass__apiclass', name:'KnowledgeGraph'})
        CREATE (nA:__Entity__ {id:'neighbourA__concept', name:'NeighbourA'})
        CREATE (nFar:__Entity__ {id:'neighbourFar__concept', name:'NeighbourFar'})
        CREATE (hub:__Entity__ {id:'hub__concept', name:'HubEntity'})
        CREATE (sdk)-[:RELATES {rel_type:'SUPPORTS', fact:'sdk supports newclass', embedding:[1.0,0.0,0.0,0.0]}]->(newc)
        CREATE (sdk)-[:RELATES {rel_type:'SUPPORTS', fact:'sdk supported oldclass', embedding:[0.0,1.0,0.0,0.0]}]->(oldc)
        CREATE (sdk)-[:RELATES {rel_type:'RELATED_TO', fact:'sdk relates to neighbourA'}]->(nA)
        CREATE (sdk)-[:RELATES {rel_type:'RELATED_TO', fact:'sdk relates to neighbourFar'}]->(nFar)
        CREATE (sdk)-[:RELATES {rel_type:'RELATED_TO', fact:'sdk relates to hub'}]->(hub)
        """,
        {"doc": DOC},
    )
    # `hub` becomes a genuine hub: 15 extra RELATES edges to fresh sinks.
    for i in range(15):
        await g.run(
            "MATCH (hub:__Entity__ {id:'hub__concept'}) "
            "CREATE (s:__Entity__ {id:$sid, name:$sid}) "
            "CREATE (hub)-[:RELATES {rel_type:'RELATED_TO', fact:'filler'}]->(s)",
            {"sid": f"hubsink{i}__concept"},
        )
    # 20 unrelated filler edges so the 95th-percentile threshold is meaningful.
    for i in range(20):
        await g.run(
            "CREATE (a:__Entity__ {id:$a, name:$a})-[:RELATES {rel_type:'RELATED_TO', fact:'filler'}]->"
            "(b:__Entity__ {id:$b, name:$b})",
            {"a": f"filler{i}a__concept", "b": f"filler{i}b__concept"},
        )


async def _write_answer(g, qid, qtext, qemb, aid, fact_keys, used_chunk=None, used_entity=None):
    await g.run(
        "MERGE (q:Question {id:$qid}) ON CREATE SET q.text=$qtext, q.embedding=vecf32($qemb), "
        "q.source='generated', q.created_at=timestamp() "
        "CREATE (a:Answer {id:$aid, text:'x', status:'current', abstained:false, fact_keys:$fact_keys, "
        "entity_ids:[], doc_ids:[], created_at:timestamp()}) "
        "CREATE (q)-[:ANSWERED_BY]->(a)",
        {"qid": qid, "qtext": qtext, "qemb": qemb, "aid": aid, "fact_keys": fact_keys},
    )
    if used_chunk:
        await g.run(
            "MATCH (a:Answer {id:$aid}), (c:Chunk {id:$cid}) CREATE (a)-[:USED_CHUNK]->(c)",
            {"aid": aid, "cid": used_chunk},
        )
    if used_entity:
        await g.run(
            "MATCH (a:Answer {id:$aid}), (e:__Entity__ {id:$eid}) CREATE (a)-[:USED_ENTITY]->(e)",
            {"aid": aid, "eid": used_entity},
        )


async def test_all_four_tiers(graph_factory):
    settings = Settings(
        embed_dimensions=4,
        t3_semantic_gate_theta=0.55,
        t4_similarity_theta=0.55,
        hub_degree_percentile=0.95,
    )
    g = await graph_factory("impact", settings=settings)
    await Ledger(g, settings).ensure_indexes()
    await _build_graph(g)

    # T1: used a chunk of the changed doc. Also matches T2's fact key —
    # must still be reported as T1 in the final merge (priority order).
    await _write_answer(
        g, "q1", "T1 question", [0, 0, 0, 1], "a1",
        fact_keys=["graphrag_sdk__product|SUPPORTS|oldclass__apiclass"],
        used_chunk="chunk-api",
    )
    # T2 only: fact key matches a removed fact, no chunk/entity link.
    await _write_answer(
        g, "q2", "T2 question", [0, 0, 1, 0], "a2",
        fact_keys=["graphrag_sdk__product|SUPPORTS|oldclass__apiclass"],
    )
    # T3: neighbour of a changed entity, question embedding close to the changed-fact pool.
    await _write_answer(g, "q3", "T3 close question", [0.9, 0.1, 0, 0], "a3", [], used_entity="neighbourA__concept")
    # T3 candidate gated OUT: neighbour link exists, but question is semantically far.
    await _write_answer(g, "q4", "T3 far question", [0, 0, 0, 1], "a4", [], used_entity="neighbourFar__concept")
    # T4 only: no entity/chunk link at all, but question is close to the ADDED fact's embedding.
    await _write_answer(g, "q5", "T4 close question", [0.95, 0.05, 0, 0], "a5", [])
    # Hub-dampened: `hub` is a 1-hop neighbour of the changed entity, but dampened out of T3.
    # Embedding kept far so T4's independent vector search doesn't also (legitimately) catch it.
    await _write_answer(g, "q6", "hub question", [0, 0, 0, 1], "a6", [], used_entity="hub__concept")
    # Control: unrelated to everything. Must never be flagged.
    await _write_answer(g, "q7", "control question", [0, 1, 1, 0], "a7", [])

    diff = FactDiff(
        added={
            "graphrag_sdk__product|SUPPORTS|newclass__apiclass": FactRow(
                "graphrag_sdk__product", "SUPPORTS", "newclass__apiclass",
                "sdk supports newclass", [1.0, 0.0, 0.0, 0.0],
            )
        },
        removed={
            "graphrag_sdk__product|SUPPORTS|oldclass__apiclass": FactRow(
                "graphrag_sdk__product", "SUPPORTS", "oldclass__apiclass",
                "sdk supported oldclass", [0.0, 1.0, 0.0, 0.0],
            )
        },
        modified={},
        changed_entity_ids={"graphrag_sdk__product", "newclass__apiclass", "oldclass__apiclass"},
    )

    result = await compute_impact(g, diff, [DOC], settings=settings)
    by_id = {c.answer_id: c for c in result.candidates}

    assert by_id["a1"].tier == "T1"
    assert by_id["a2"].tier == "T2"
    assert by_id["a3"].tier == "T3"
    assert "a4" not in by_id, "semantic gate (theta3) should have filtered this out"
    assert by_id["a5"].tier == "T4"
    assert "a6" not in by_id, "hub dampening should have filtered this out"
    assert "a7" not in by_id, "control answer must never be flagged"
    assert result.truncated is False
