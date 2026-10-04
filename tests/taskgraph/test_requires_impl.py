"""requires_impl: needing a working implementation rather than an interface."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from aqours_code.taskgraph import derive_edges, dump_graph, load_graph, validate
from taskgraph_support import make_edge, make_graph, make_node

LOAD = "store.py::JobStore.load_unfinished"


def errors_with(report, code):
    return [issue for issue in report.errors if issue.code == code]


def warnings_with(report, code):
    return [issue for issue in report.warnings if issue.code == code]


def edge_tuples(graph):
    return [(edge.from_, edge.to, edge.type, edge.source) for edge in graph.edges]


# ── schema ──

def test_requires_impl_round_trips(tmp_path):
    graph = make_graph([
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("G", modify=("runner.py",), requires=("runner.py::run_loop",),
                  requires_impl=(LOAD,)),
    ], [make_edge("B", "G")])
    path = tmp_path / "graph.json"
    dump_graph(graph, path)
    reloaded = load_graph(path)
    assert reloaded == graph
    assert reloaded.nodes[1].requires_impl == [LOAD]
    assert '"requires_impl"' in path.read_text(encoding="utf-8")


def test_requires_impl_defaults_to_empty():
    graph = make_graph([make_node("A", modify=("a.py",))])
    assert graph.nodes[0].requires_impl == []


def test_symbol_in_requires_and_requires_impl_is_rejected():
    with pytest.raises(ValidationError, match="both requires and requires_impl"):
        make_graph([make_node("G", modify=("a.py",), requires=(LOAD,),
                              requires_impl=(LOAD,))])


@pytest.mark.parametrize("bad", ["store.py:load", "README.md::x", "store.py::1x"])
def test_malformed_requires_impl_symbol_is_rejected(bad):
    with pytest.raises(ValidationError):
        make_graph([make_node("G", modify=("a.py",), requires_impl=(bad,))])


def test_duplicate_requires_impl_symbol_is_rejected():
    with pytest.raises(ValidationError, match="duplicate symbols"):
        make_graph([make_node("G", modify=("a.py",), requires_impl=(LOAD, LOAD))])


def test_v8_checks_requires_impl_that_bypassed_the_schema():
    graph = make_graph([make_node("G", modify=("a.py",))])
    graph.nodes[0].requires_impl.append("a.py:x")
    issues = errors_with(validate(graph), "V8")
    assert len(issues) == 1 and "requires_impl" in issues[0].message


# ── V12 ──

def test_v12_passes_when_implementer_is_ancestor(toy_index):
    graph = make_graph([
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ], [make_edge("B", "G")])
    report = validate(graph, toy_index)
    assert report.ok, report.format()


def test_v12_implementer_not_ancestor(toy_index):
    graph = make_graph([
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ])
    issues = errors_with(validate(graph, toy_index), "V12")
    assert len(issues) == 1 and issues[0].nodes == ["G", "B"]
    assert issues[0].message == (
        f"requires the implementation of {LOAD}, but B is not an ancestor of G "
        "(missing edge?)")


def test_v12_implementer_listed_only_in_edit_set_symbols_counts(toy_index):
    graph = make_graph([
        make_node("B1", modify=("store.py",), provides=(LOAD,)),
        make_node("B2", modify=("store.py",), symbols=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ], [make_edge("B1", "G"), make_edge("B1", "B2", "order")])
    issues = errors_with(validate(graph, toy_index), "V12")
    assert len(issues) == 1 and issues[0].nodes == ["G", "B2"]
    assert "B2 is not an ancestor of G" in issues[0].message


def test_v12_only_declared_by_contract(toy_index):
    graph = make_graph([
        make_node("C", kind="contract", modify=("store.py",), provides=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ], [make_edge("C", "G", "interface")])
    issues = errors_with(validate(graph, toy_index), "V12")
    assert len(issues) == 1 and issues[0].nodes == ["G", "C"]
    assert f"{LOAD} is only declared by contract C" in issues[0].message
    assert "no implement node implements it" in issues[0].message


def test_v12_existing_symbol_without_implementers_passes(toy_index):
    graph = make_graph([
        make_node("G", modify=("runner.py",), requires_impl=("store.py::JobStore.add",)),
    ])
    assert validate(graph, toy_index).ok


def test_v12_missing_symbol_without_any_source(toy_index):
    graph = make_graph([make_node("G", modify=("runner.py",), requires_impl=(LOAD,))])
    issues = errors_with(validate(graph, toy_index), "V12")
    assert len(issues) == 1 and issues[0].nodes == ["G"]
    assert "neither defined at the base commit nor implemented by any node" in (
        issues[0].message)


def test_v12_skipped_without_index():
    graph = make_graph([
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ])
    report = validate(graph)
    assert not errors_with(report, "V12")
    skipped = warnings_with(report, "V12")
    assert len(skipped) == 1 and "skipped" in skipped[0].message
    assert [issue.code for issue in report.warnings if "skipped" in issue.message] == [
        "V3", "V6", "V12", "W5"]


# ── derive ──

def test_derive_contract_interface_and_implementation_edges(toy_index):
    graph = make_graph([
        make_node("A", kind="contract", modify=("store.py",), provides=(LOAD,)),
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("C", modify=("runner.py",), requires=(LOAD,)),
        make_node("G", create=("tests/test_integration.py",), requires_impl=(LOAD,)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    edges = edge_tuples(derived)
    assert ("A", "C", "interface", "derived") in edges
    assert ("B", "G", "full", "derived") in edges
    assert ("A", "B", "order", "derived") in edges
    assert not [e for e in entries if "multiple providers" in e.reason]
    implementation = [e for e in derived.edges if e.from_ == "B" and e.to == "G"][0]
    assert "requires implementation" in implementation.reason and LOAD in implementation.reason
    report = validate(derived, toy_index)
    assert report.ok, report.format()
    assert not report.warnings, report.format()


def test_derive_requires_prefers_the_contract_over_an_implementer(toy_index):
    graph = make_graph([
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("A", kind="contract", modify=("store.py",), provides=(LOAD,)),
        make_node("X", modify=("runner.py",), requires=(LOAD,)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert ("A", "X", "interface", "derived") in edge_tuples(derived)
    assert not [e for e in derived.edges if e.from_ == "B" and e.to == "X"]
    assert not [e for e in entries if "multiple providers" in e.reason]
    assert validate(derived, toy_index).ok


def test_derive_requires_with_two_contract_providers_stays_ambiguous(toy_index):
    graph = make_graph([
        make_node("A1", kind="contract", modify=("store.py",), provides=(LOAD,)),
        make_node("A2", kind="contract", modify=("store.py",), provides=(LOAD,)),
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("X", modify=("runner.py",), requires=(LOAD,)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert not [e for e in derived.edges if e.to == "X"]
    others = [e for e in entries if e.action == "other"]
    assert len(others) == 1 and others[0].nodes == ["X", "A1", "A2"]
    assert "multiple providers" in others[0].reason


def test_derive_requires_impl_adds_edges_from_every_implementer(toy_index):
    graph = make_graph([
        make_node("B1", modify=("store.py",), provides=(LOAD,)),
        make_node("B2", modify=("store.py",), symbols=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ])
    derived, _ = derive_edges(graph, toy_index)
    edges = edge_tuples(derived)
    assert ("B1", "G", "full", "derived") in edges
    assert ("B2", "G", "full", "derived") in edges
    assert validate(derived, toy_index).ok


def test_derive_requires_impl_from_downstream_implementer_is_a_cycle(toy_index):
    graph = make_graph([
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
        make_node("B", modify=("store.py",), provides=(LOAD,)),
    ], [make_edge("G", "B")])
    derived, entries = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("G", "B", "full", "manual")]
    assert len(entries) == 1 and entries[0].action == "other"
    assert "cycle" in entries[0].reason and "requires implementation" in entries[0].reason
    assert errors_with(validate(derived, toy_index), "V12")


def test_derive_requires_impl_without_implementers_adds_no_edge(toy_index):
    graph = make_graph([
        make_node("C", kind="contract", modify=("store.py",), provides=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert derived.edges == [] and entries == []
    assert errors_with(validate(derived, toy_index), "V12")


# ── warnings ──

def test_w4_requires_impl_over_order_only_edge(toy_index):
    graph = make_graph([
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ], [make_edge("B", "G", "order")])
    report = validate(graph, toy_index)
    assert report.ok
    issues = warnings_with(report, "W4")
    assert len(issues) == 1 and issues[0].nodes == ["B", "G"]
    assert f"requires the implementation of {LOAD}" in issues[0].message


def test_w4_not_raised_for_requires_impl_over_full_edge(toy_index):
    graph = make_graph([
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ], [make_edge("B", "G", "full")])
    assert not warnings_with(validate(graph, toy_index), "W4")


def test_w2_counts_requires_impl_as_use(toy_index):
    graph = make_graph([
        make_node("B", modify=("store.py",), provides=(LOAD,)),
        make_node("G", modify=("runner.py",), requires_impl=(LOAD,)),
    ], [make_edge("B", "G")])
    assert not warnings_with(validate(graph, toy_index), "W2")


def test_w5_modifier_listed_only_in_edit_set_symbols(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::run_loop",)),
        make_node("M", modify=("runner.py",), symbols=("runner.py::run_loop",)),
    ])
    issues = warnings_with(validate(graph, toy_index), "W5")
    assert len(issues) == 1 and issues[0].nodes == ["X", "M"]


def test_w5_ignores_requires_impl(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires_impl=("runner.py::run_loop",)),
        make_node("M", modify=("runner.py",), symbols=("runner.py::run_loop",)),
    ])
    report = validate(graph, toy_index)
    assert not warnings_with(report, "W5")
    assert errors_with(report, "V12")
