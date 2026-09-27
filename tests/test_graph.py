from core.graph import ONTOLOGY_GRAPH_SUFFIX, GraphHandle, list_graphs


async def test_copy_and_delete_carry_the_paired_ontology_graph(graph: GraphHandle, falkordb_or_skip):
    await graph.run("CREATE (:Probe {n: 1})")
    onto = GraphHandle(graph.graph_name + ONTOLOGY_GRAPH_SUFFIX, falkordb_or_skip)
    await onto.run("CREATE (:__OntologyProbe__ {v: 1})")

    copy = await graph.copy_to(graph.graph_name + "_copy")
    names = set(await list_graphs(falkordb_or_skip))
    assert copy.graph_name in names
    assert copy.graph_name + ONTOLOGY_GRAPH_SUFFIX in names

    await copy.delete()
    await graph.delete()
    names = set(await list_graphs(falkordb_or_skip))
    for n in (copy.graph_name, graph.graph_name):
        assert n not in names and n + ONTOLOGY_GRAPH_SUFFIX not in names
    await copy.close()
    await onto.close()
