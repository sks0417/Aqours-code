from __future__ import annotations

from aqours_code.taskgraph import derive_edges, load_graph, validate
from aqours_code.taskgraph.cli import main
from taskgraph_support import make_edge, make_graph, make_node


def edge_tuples(graph):
    return [(edge.from_, edge.to, edge.type, edge.source) for edge in graph.edges]


def test_adds_missing_dependency_edge(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::purge_jobs",)),
        make_node("P", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
    ])
    assert not validate(graph, toy_index).ok
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("P", "X", "full", "derived")]
    assert "runner.py::purge_jobs" in derived.edges[0].reason
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


def test_creator_is_ordered_before_modifier_listed_first(toy_index):
    graph = make_graph([
        make_node("B", modify=("util.py",)),
        make_node("A", create=("util.py",)),
    ])
    assert "V3" in validate(graph, toy_index).codes()
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("A", "B", "order", "derived")]
    assert [entry.action for entry in entries] == ["add_edge"]
    assert validate(derived, toy_index).ok


def test_creator_rule_takes_priority_over_contract_rule(toy_index):
    graph = make_graph([
        make_node("I", create=("util.py",)),
        make_node("C", kind="contract", modify=("util.py",)),
    ])
    derived, _ = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("I", "C", "order", "derived")]
    assert validate(derived, toy_index).ok


def test_conflicting_creation_direction_adds_no_edge(toy_index):
    graph = make_graph([
        make_node("A", create=("x_new.py",), modify=("y_new.py",)),
        make_node("B", create=("y_new.py",), modify=("x_new.py",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert derived.edges == []
    assert len(entries) == 1 and entries[0].action == "other"
    assert entries[0].nodes == ["A", "B"]
    assert "conflicting creation direction" in entries[0].reason
    assert "x_new.py" in entries[0].reason and "y_new.py" in entries[0].reason
    assert not validate(derived, toy_index).ok


def test_context_file_creator_is_ordered_before_reader(toy_index):
    graph = make_graph([
        make_node("A", create=("contract.py",)),
        make_node("B", modify=("store.py",), context_files=("contract.py",)),
    ])
    assert "V3" in validate(graph, toy_index).codes()
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("A", "B", "full", "derived")]
    assert "context file contract.py" in derived.edges[0].reason
    assert [entry.action for entry in entries] == ["add_edge"]
    report = validate(derived, toy_index)
    assert report.ok, report.format()


def test_context_file_from_contract_creator_gets_interface_edge(toy_index):
    graph = make_graph([
        make_node("B", modify=("store.py",), context_files=("contract.py",)),
        make_node("C", kind="contract", create=("contract.py",)),
    ])
    derived, _ = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("C", "B", "interface", "derived")]
    assert validate(derived, toy_index).ok


def test_context_file_created_by_the_node_itself_gets_no_edge(toy_index):
    graph = make_graph([
        make_node("B", create=("contract.py",), context_files=("contract.py",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert derived.edges == [] and entries == []
    assert "V3" in validate(derived, toy_index).codes()


def test_context_file_created_downstream_is_not_derived_because_of_cycle(toy_index):
    graph = make_graph([
        make_node("B", modify=("store.py",), context_files=("contract.py",)),
        make_node("A", create=("contract.py",)),
    ], [make_edge("B", "A")])
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("B", "A", "full", "manual")]
    assert len(entries) == 1 and entries[0].action == "other"
    assert "cycle" in entries[0].reason and "contract.py" in entries[0].reason
    assert "V3" in validate(derived, toy_index).codes()


def test_context_file_with_several_creators_is_left_to_v11(toy_index):
    graph = make_graph([
        make_node("A1", create=("contract.py",)),
        make_node("A2", create=("contract.py",)),
        make_node("B", modify=("store.py",), context_files=("contract.py",)),
    ])
    derived, _ = derive_edges(graph, toy_index)
    assert not [edge for edge in derived.edges if edge.to == "B"]
    assert "V11" in validate(derived, toy_index).codes()


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
        make_node("A", modify=("runner.py",), requires=("runner.py::purge_jobs",)),
        make_node("B", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    # The dependency edge B -> A already orders the two edits of runner.py.
    assert edge_tuples(derived) == [("B", "A", "full", "derived")]
    assert len(entries) == 1
    assert validate(derived, toy_index).ok


def test_edge_that_would_create_cycle_is_not_added(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::purge_jobs",)),
        make_node("P", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
    ], [make_edge("X", "P", "order")])
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("X", "P", "order", "manual")]
    assert len(entries) == 1 and entries[0].action == "other"
    assert "cycle" in entries[0].reason
    assert "V6" in validate(derived, toy_index).codes()


def test_multiple_providers_are_left_to_the_validator(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::purge_jobs",)),
        make_node("P1", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
        make_node("P2", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    # Both providers edit runner.py, so step 2 orders them; no edge reaches X.
    assert edge_tuples(derived) == [("P1", "P2", "order", "derived")]
    assert [entry.action for entry in entries] == ["add_edge", "other"]
    assert entries[1].nodes == ["X", "P1", "P2"]
    assert "runner.py::purge_jobs" in entries[1].reason
    assert "multiple providers, edge not derived" in entries[1].reason
    assert derived.revision_log == entries
    assert "V6" in validate(derived, toy_index).codes()


def test_multiple_provider_entry_dropped_when_later_dependency_edge_resolves_it(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",),
                  requires=("runner.py::b", "runner.py::a")),
        make_node("P", modify=("runner.py",),
                  provides=("runner.py::a", "runner.py::b")),
        make_node("Q", modify=("runner.py",), provides=("runner.py::b",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    # P and Q both provide runner.py::b, so both edit runner.py and step 2 also
    # orders them (P -> Q); only the entries about X matter here.
    assert edge_tuples(derived) == [("P", "X", "full", "derived"),
                                    ("P", "Q", "order", "derived")]
    assert [entry.action for entry in entries] == ["add_edge", "add_edge"]
    assert validate(derived, toy_index).ok


def test_multiple_provider_entry_dropped_when_order_edge_resolves_it(toy_index):
    graph = make_graph([
        make_node("P1", modify=("store.py",), provides=("store.py::JobStore.purge",)),
        make_node("X", modify=("store.py",), requires=("store.py::JobStore.purge",)),
        make_node("P2", modify=("store.py",), provides=("store.py::JobStore.purge",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    # All three edit store.py, so step 2 adds P1 -> X (and orders P2 too).
    # That makes P1 an ancestor of X, so no multi-provider entry is written.
    assert ("P1", "X", "order", "derived") in edge_tuples(derived)
    assert {entry.action for entry in entries} == {"add_edge"}
    report = validate(derived, toy_index)
    assert not [issue for issue in report.errors if issue.code == "V6"]


def test_multiple_provider_entry_kept_when_order_edge_points_away(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("store.py::JobStore.purge",)),
        make_node("P1", modify=("store.py",), provides=("store.py::JobStore.purge",)),
        make_node("P2", modify=("store.py",), provides=("store.py::JobStore.purge",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    # The order edges X -> P1 and X -> P2 do not make a provider an ancestor of X.
    assert ("X", "P1", "order", "derived") in edge_tuples(derived)
    assert [entry.action for entry in entries] == ["add_edge"] * 3 + ["other"]
    assert entries[-1].nodes == ["X", "P1", "P2"]
    assert "multiple providers, edge not derived" in entries[-1].reason


def test_unresolved_multiple_provider_entries_come_after_edge_entries(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::purge_jobs",)),
        make_node("P1", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
        make_node("P2", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
        make_node("C", modify=("runner.py",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert [entry.action for entry in entries] == ["add_edge"] * 3 + ["other"]
    assert [entry.nodes for entry in entries[:3]] == [["P1", "P2"], ["P1", "C"], ["P2", "C"]]
    assert entries[3].nodes == ["X", "P1", "P2"]
    assert derived.revision_log == entries


def test_multiple_providers_with_one_ancestor_need_no_entry(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::purge_jobs",)),
        make_node("P1", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
        make_node("P2", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
    ], [make_edge("P1", "X")])
    derived, entries = derive_edges(graph, toy_index)
    # Both providers edit runner.py, so the only entry is the order edge
    # P1 -> P2; no multi-provider entry is written for X.
    assert [(entry.action, entry.nodes) for entry in entries] == [
        ("add_edge", ["P1", "P2"])]
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
