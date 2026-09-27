# Integration test for core/diff.py's fact diff, using a scenario shaped
# exactly like the design's own running example (PR #479's KnowledgeGraph ->
# GraphRAG / GraphSchema rewrite — see SYSTEM_DESIGN.md §0, §8).

from __future__ import annotations

from core.diff import diff_facts

DOC = "docs/api.mdx"


async def _seed_old(g):
    await g.run(
        """
        CREATE (d:Document {id: $doc})
        CREATE (c:Chunk {id:'c-old', text:'old chunk'})
        CREATE (d)-[:PART_OF]->(c)
        CREATE (sdk:__Entity__ {id:'graphrag_sdk__product', name:'GraphRAG SDK'})
        CREATE (kg:__Entity__ {id:'knowledgegraph__apiclass', name:'KnowledgeGraph'})
        CREATE (ont:__Entity__ {id:'ontology__concept', name:'ontology'})
        CREATE (fdb:__Entity__ {id:'falkordb__product', name:'FalkorDB'})
        CREATE (v1:__Entity__ {id:'v1_0__version', name:'v1.0'})
        CREATE (sdk)-[:MENTIONED_IN]->(c)
        CREATE (kg)-[:MENTIONED_IN]->(c)
        CREATE (ont)-[:MENTIONED_IN]->(c)
        CREATE (fdb)-[:MENTIONED_IN]->(c)
        CREATE (v1)-[:MENTIONED_IN]->(c)
        CREATE (sdk)-[:RELATES {rel_type:'SUPPORTS', fact:'GraphRAG SDK KnowledgeGraph builds a knowledge graph'}]->(kg)
        CREATE (kg)-[:RELATES {rel_type:'REQUIRES', fact:'requires an ontology dict'}]->(ont)
        CREATE (sdk)-[:RELATES {rel_type:'PART_OF_PRODUCT', fact:'ships as part of FalkorDB'}]->(fdb)
        CREATE (sdk)-[:RELATES {rel_type:'INTRODUCED_IN', fact:'released in 2024'}]->(v1)
        """,
        {"doc": DOC},
    )


async def _seed_new(g):
    await g.run(
        """
        CREATE (d:Document {id: $doc})
        CREATE (c:Chunk {id:'c-new', text:'new chunk'})
        CREATE (d)-[:PART_OF]->(c)
        CREATE (sdk:__Entity__ {id:'graphrag_sdk__product', name:'GraphRAG SDK'})
        CREATE (gr:__Entity__ {id:'graphrag__apiclass', name:'GraphRAG'})
        CREATE (gs:__Entity__ {id:'graphschema__apiclass', name:'GraphSchema'})
        CREATE (fdb:__Entity__ {id:'falkordb__product', name:'FalkorDB'})
        CREATE (v1:__Entity__ {id:'v1_0__version', name:'v1.0'})
        CREATE (sdk)-[:MENTIONED_IN]->(c)
        CREATE (gr)-[:MENTIONED_IN]->(c)
        CREATE (gs)-[:MENTIONED_IN]->(c)
        CREATE (fdb)-[:MENTIONED_IN]->(c)
        CREATE (v1)-[:MENTIONED_IN]->(c)
        CREATE (sdk)-[:RELATES {rel_type:'SUPPORTS', fact:'GraphRAG SDK GraphRAG class builds a knowledge graph'}]->(gr)
        CREATE (gr)-[:RELATES {rel_type:'REQUIRES', fact:'requires a GraphSchema'}]->(gs)
        CREATE (sdk)-[:RELATES {rel_type:'PART_OF_PRODUCT', fact:'ships as part of FalkorDB'}]->(fdb)
        CREATE (sdk)-[:RELATES {rel_type:'INTRODUCED_IN', fact:'released in 2024, now stable'}]->(v1)
        """,
        {"doc": DOC},
    )


async def test_diff_facts_added_removed_modified_and_disappeared(graph_factory):
    old = await graph_factory("old")
    new = await graph_factory("new")
    await _seed_old(old)
    await _seed_new(new)

    diff = await diff_facts(old, new, [DOC], embedder=None)

    assert set(diff.added) == {
        "graphrag_sdk__product|SUPPORTS|graphrag__apiclass",
        "graphrag__apiclass|REQUIRES|graphschema__apiclass",
    }
    assert set(diff.removed) == {
        "graphrag_sdk__product|SUPPORTS|knowledgegraph__apiclass",
        "knowledgegraph__apiclass|REQUIRES|ontology__concept",
    }
    assert set(diff.modified) == {"graphrag_sdk__product|INTRODUCED_IN|v1_0__version"}

    # An identical fact on both sides must never show up in any diff bucket.
    unchanged_key = "graphrag_sdk__product|PART_OF_PRODUCT|falkordb__product"
    assert unchanged_key not in diff.added
    assert unchanged_key not in diff.removed
    assert unchanged_key not in diff.modified

    assert diff.disappeared_entity_ids == {"knowledgegraph__apiclass", "ontology__concept"}
    assert "graphrag__apiclass" in diff.changed_entity_ids
    assert "graphschema__apiclass" in diff.changed_entity_ids


async def test_diff_facts_with_stored_embedding_similarity_suppresses_rephrasing(graph_factory):
    """A fact whose stored RELATES.embedding is near-identical on both sides
    counts as unchanged even though the text differs — the re-extraction
    noise filter (§7.3 step 4) — using the embedding graphrag_sdk's own
    finalize() already wrote, no embedder call needed (see q2_scope_facts's
    header comment)."""
    old = await graph_factory("old")
    new = await graph_factory("new")
    doc = "docs/rephrase.mdx"
    await old.run(
        """
        CREATE (d:Document {id:$doc})-[:PART_OF]->(c:Chunk {id:'c1', text:'x'})
        CREATE (a:__Entity__ {id:'a__x', name:'A'})-[:MENTIONED_IN]->(c)
        CREATE (b:__Entity__ {id:'b__x', name:'B'})-[:MENTIONED_IN]->(c)
        CREATE (a)-[:RELATES {rel_type:'RELATED_TO', fact:'A relates to B', embedding:[1.0,0.0]}]->(b)
        """,
        {"doc": doc},
    )
    await new.run(
        """
        CREATE (d:Document {id:$doc})-[:PART_OF]->(c:Chunk {id:'c2', text:'x'})
        CREATE (a:__Entity__ {id:'a__x', name:'A'})-[:MENTIONED_IN]->(c)
        CREATE (b:__Entity__ {id:'b__x', name:'B'})-[:MENTIONED_IN]->(c)
        CREATE (a)-[:RELATES {rel_type:'RELATED_TO', fact:'A has a relationship with B', embedding:[0.999,0.001]}]->(b)
        """,
        {"doc": doc},
    )

    diff = await diff_facts(old, new, [doc], embedder=None)
    assert "a__x|RELATED_TO|b__x" not in diff.modified
    assert "a__x|RELATED_TO|b__x" not in diff.added
    assert "a__x|RELATED_TO|b__x" not in diff.removed
