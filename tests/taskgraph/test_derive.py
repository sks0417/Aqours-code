from __future__ import annotations

from aqours_code.taskgraph import derive_edges, load_graph, validate
from aqours_code.taskgraph.cli import main
from taskgraph_support import make_edge, make_graph, make_node


def edge_tuples(graph):
    return [(edge.from_, edge.to, edge.type, edge.source) for edge in graph.edges]


def test_adds_missing_dependency_edge(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("store.py::JobStore.purge",)),
        make_node("P", modify=("runner.py",), provides=("store.py::JobStore.purge",)),
    ])
    assert not validate(graph, toy_index).ok
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("P", "X", "full", "derived")]
    assert "store.py::JobStore.purge" in derived.edges[0].reason
    assert [entry.action for entry in entries] == ["add_edge"]
    assert validate(derived, toy_index).ok


def test_contract_provider_gets_interface_edge(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("models.py::JobStatus.FAILED",)),
        make_node("C", kind="contract", modify=("models.py",),
                  provides=("models.py::JobStatus.FAILED",)),
    ])
    derived, _ = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("C", "X", "interface", "derived")]
    assert validate(derived, toy_index).ok


def test_adds_missing_order_edge_in_list_order(toy_index):
    graph = make_graph([
        make_node("C", modify=("runner.py",)),
        make_node("D", modify=("runner.py",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("C", "D", "order", "derived")]
    assert "runner.py" in derived.edges[0].reason
    assert entries[0].nodes == ["C", "D"]
    assert validate(derived, toy_index).ok


def test_contract_node_is_ordered_first(toy_index):
    graph = make_graph([
        make_node("impl", modify=("models.py",)),
        make_node("contract", kind="contract", modify=("models.py",)),
    ])
    derived, _ = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("contract", "impl", "order", "derived")]


def test_adds_dependency_then_order_edges(toy_index):
    graph = make_graph([
        make_node("A", modify=("runner.py",), requires=("store.py::JobStore.purge",)),
        make_node("B", modify=("store.py",), provides=("store.py::JobStore.purge",)),
        make_node("C", modify=("runner.py",)),
    ])
    report = validate(graph, toy_index)
    assert set(report.codes()) == {"V5", "V6"}
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [
        ("B", "A", "full", "derived"),
        ("A", "C", "order", "derived"),
    ]
    assert len(entries) == 2
    assert validate(derived, toy_index).ok


def test_existing_ancestry_makes_order_edge_unnecessary(toy_index):
    graph = make_graph([
        make_node("A", modify=("runner.py",), requires=("store.py::JobStore.purge",)),
        make_node("B", modify=("runner.py",), provides=("store.py::JobStore.purge",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    # The dependency edge B -> A already orders the two edits of runner.py.
    assert edge_tuples(derived) == [("B", "A", "full", "derived")]
    assert len(entries) == 1
    assert validate(derived, toy_index).ok


def test_edge_that_would_create_cycle_is_not_added(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("store.py::JobStore.purge",)),
        make_node("P", modify=("runner.py",), provides=("store.py::JobStore.purge",)),
    ], [make_edge("X", "P", "order")])
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("X", "P", "order", "manual")]
    assert len(entries) == 1 and entries[0].action == "other"
    assert "cycle" in entries[0].reason
    assert "V6" in validate(derived, toy_index).codes()


def test_multiple_providers_are_left_to_the_validator(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("store.py::JobStore.purge",)),
        make_node("P1", modify=("runner.py",), provides=("store.py::JobStore.purge",)),
        make_node("P2", modify=("models.py",), provides=("store.py::JobStore.purge",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert derived.edges == []
    assert len(entries) == 1
    assert entries[0].action == "other"
    assert entries[0].nodes == ["X", "P1", "P2"]
    assert "store.py::JobStore.purge" in entries[0].reason
    assert "multiple providers, edge not derived" in entries[0].reason
    assert derived.revision_log == entries
    assert "V6" in validate(derived, toy_index).codes()


def test_multiple_providers_with_one_ancestor_need_no_entry(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("store.py::JobStore.purge",)),
        make_node("P1", modify=("runner.py",), provides=("store.py::JobStore.purge",)),
        make_node("P2", modify=("models.py",), provides=("store.py::JobStore.purge",)),
    ], [make_edge("P1", "X")])
    derived, entries = derive_edges(graph, toy_index)
    assert entries == []
    assert validate(derived, toy_index).ok


def test_symbols_in_repo_need_no_edge(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::run_loop",)),
        make_node("P", modify=("runner.py",), provides=("runner.py::run_loop",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert derived.edges == [] and entries == []


def test_original_graph_is_not_modified(toy_index):
    graph = make_graph([
        make_node("A", modify=("runner.py",), requires=("store.py::JobStore.purge",)),
        make_node("B", modify=("store.py",), provides=("store.py::JobStore.purge",)),
        make_node("C", modify=("runner.py",)),
    ])
    before = graph.model_dump()
    derived, entries = derive_edges(graph, toy_index)
    assert graph.model_dump() == before
    assert derived is not graph
    assert derived.revision_log == entries


def test_cli_derive_writes_valid_graph(tmp_path, toy_repo, capsys):
    from aqours_code.taskgraph import dump_graph

    graph = make_graph([
        make_node("C", modify=("runner.py",)),
        make_node("D", modify=("runner.py",)),
    ]).model_copy(update={"base_commit": toy_repo.commit})
    source = tmp_path / "graph.json"
    out = tmp_path / "derived.json"
    dump_graph(graph, source)
    assert main(["derive", str(source), "--repo", str(toy_repo.path),
                 "--out", str(out)]) == 0
    assert edge_tuples(load_graph(out)) == [("C", "D", "order", "derived")]
    assert "[add_edge]" in capsys.readouterr().out
    assert main(["validate", str(out), "--repo", str(toy_repo.path)]) == 0


def test_cli_index_and_export_schema(tmp_path, toy_repo, capsys):
    assert main(["index", "--repo", str(toy_repo.path), "--commit", toy_repo.commit]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "runner.py::run_loop" in lines and lines == sorted(lines)
    target = tmp_path / "schema.json"
    assert main(["export-schema", "--out", str(target)]) == 0
    assert target.is_file()
