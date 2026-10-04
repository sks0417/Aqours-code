"""Boundary cases for the task graph schema, index, validator and derive.

Tests marked ``xfail(strict=True)`` describe required behaviour that the
current implementation does not have yet; they turn into failures (XPASS)
once the fix lands, so the marker must be removed together with the fix.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from aqours_code.taskgraph import (
    Graph,
    build_index,
    derive_edges,
    dump_graph,
    load_graph,
    parse_symbol,
    validate,
)
from aqours_code.taskgraph.repo_index import extract_symbols
from taskgraph_support import EXAMPLE_GRAPH, commit_files, make_edge, make_graph, make_node


def codes(report, code):
    return [issue for issue in report.errors if issue.code == code]


def warnings_with(report, code):
    return [issue for issue in report.warnings if issue.code == code]


def edge_tuples(graph):
    return [(edge.from_, edge.to, edge.type, edge.source) for edge in graph.edges]


# ── schema: symbols, ids, numbers ──

@pytest.mark.parametrize("good", [
    "pkg/sub/mod.py::Outer.Inner.method",
    "pkg/__init__.py::VERSION",
    "a.py::_private",
])
def test_parse_symbol_accepts(good):
    assert str(parse_symbol(good)) == good


@pytest.mark.parametrize("bad", [
    "a.py::", "::x", "a.py::x::y", "a.py::A..b", "a.py::x.", "a.py::.x",
    "a.py:: x", "a.py::1x", "a.pyi::X", "a.PY::X", "a.py", "/a.py::x",
    "pkg/../a.py::x", "pkg\\a.py::x",
])
def test_parse_symbol_rejects(bad):
    with pytest.raises(ValueError):
        parse_symbol(bad)


@pytest.mark.parametrize("node_id", ["A\n", "a b", "", "Ａ", "a.b", "a/b"])
def test_node_id_pattern_is_strict(node_id):
    with pytest.raises(ValidationError):
        make_graph([make_node(node_id, modify=("a.py",))])


@pytest.mark.parametrize("timeout", [0, -1, 1.0, "5", True, None])
def test_timeout_must_be_positive_int(timeout):
    node = make_node("A", modify=("a.py",))
    node["check"]["timeout_s"] = timeout
    with pytest.raises(ValidationError):
        make_graph([node])


def test_cross_node_modify_create_overlap_is_left_to_v5():
    # Decision 4 only forbids overlap *within* one node.
    graph = make_graph([make_node("A", create=("x.py",)), make_node("B", modify=("x.py",))])
    assert [issue.nodes for issue in codes(validate(graph), "V5")] == [["A", "B"]]


def test_duplicate_within_create_and_overlap_reported_at_load(tmp_path):
    data = json.loads(EXAMPLE_GRAPH.read_text(encoding="utf-8"))
    data["nodes"][0]["edit_set"]["create"] = ["new.py", "new.py"]
    path = tmp_path / "g.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="duplicate paths: new.py"):
        load_graph(path)


def test_invalid_json_text_is_a_validation_error(tmp_path):
    path = tmp_path / "g.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_graph(path)


def test_round_trip_keeps_revision_log_and_optional_fields(tmp_path):
    data = load_graph(EXAMPLE_GRAPH).model_dump(by_alias=True)
    data["revision_log"] = [
        {"action": "merge", "nodes": ["a", "b"], "into": "ab", "reason": "r"},
        {"action": "other", "nodes": [], "reason": "no into"},
    ]
    graph = Graph.model_validate(data)
    first, second = tmp_path / "1.json", tmp_path / "2.json"
    dump_graph(graph, first)
    dump_graph(load_graph(first), second)
    assert first.read_bytes() == second.read_bytes()
    assert load_graph(second) == graph
    assert "into" not in json.loads(second.read_text())["revision_log"][1]


# ── repo_index ──

def test_index_nested_class_init_and_non_self_targets():
    source = '''
class Outer:
    class Inner:
        def __init__(self):
            self.inner_attr = 1
            self.obj.attr = 2
            self.items[0] = 3
            other.attr = 4
    @staticmethod
    def __init__():
        pass
def f():
    LOCAL = 1
    def nested():
        pass
'''
    names = {symbol.split("::")[1] for symbol in extract_symbols(source, "m.py")}
    assert names == {"Outer", "Outer.Inner", "Outer.Inner.__init__",
                     "Outer.Inner.inner_attr", "Outer.__init__", "f"}


def test_index_skips_null_byte_file_and_keeps_odd_file_names(toy_repo):
    commit = commit_files(toy_repo.path, {
        "pkg/with space.py": "VALUE = 1\n",
        "pkg/ünïcode.py": "def f():\n    pass\n",
    }, "odd names")
    (toy_repo.path / "null_byte.py").write_bytes(b"X = 1\x00\n")
    from taskgraph_support import git
    git(toy_repo.path, "add", "null_byte.py")
    git(toy_repo.path, "commit", "-q", "-m", "null byte")
    index = build_index(toy_repo.path, "HEAD")
    assert index.has_symbol("pkg/with space.py::VALUE")
    assert index.has_symbol("pkg/ünïcode.py::f")
    assert index.has_file("null_byte.py")
    assert not any(symbol.startswith("null_byte.py::") for symbol in index.symbols)
    assert any("null_byte.py" in warning for warning in index.warnings)
    assert index.commit != commit  # HEAD was resolved to the newest sha


# ── validate ──

def test_v2_reports_each_disjoint_cycle():
    graph = make_graph(
        [make_node(n, modify=(f"{n}.py",)) for n in "ABCD"],
        [make_edge("A", "B"), make_edge("B", "A", "order"),
         make_edge("C", "D"), make_edge("D", "C")],
    )
    assert [issue.nodes for issue in codes(validate(graph), "V2")] == [["A", "B"], ["C", "D"]]


def test_v5_transitive_ancestry_orders_edits():
    graph = make_graph(
        [make_node("A", modify=("x.py",)), make_node("B", modify=("y.py",)),
         make_node("C", modify=("x.py",))],
        [make_edge("A", "B", "order"), make_edge("B", "C", "order")],
    )
    assert not codes(validate(graph), "V5")


def test_v6_transitive_order_ancestor_satisfies_requires_without_w4(toy_index):
    # Decision 1: any edge satisfies V6; W4 only looks at the direct edge.
    graph = make_graph([
        make_node("P", modify=("store.py",), provides=("store.py::JobStore.purge",)),
        make_node("M", modify=("models.py",)),
        make_node("X", modify=("runner.py",), requires=("store.py::JobStore.purge",)),
    ], [make_edge("P", "M", "order"), make_edge("M", "X", "order")])
    report = validate(graph, toy_index)
    assert report.ok, report.format()
    assert not warnings_with(report, "W4")


def test_v6_two_non_ancestor_providers_reported_once(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::purge",)),
        make_node("P", modify=("runner.py",), provides=("runner.py::purge",)),
        make_node("Q", modify=("runner.py",), provides=("runner.py::purge",)),
    ])
    issues = codes(validate(graph, toy_index), "V6")
    assert len(issues) == 1 and issues[0].nodes == ["X", "P", "Q"]


def test_w4_lists_all_shared_symbols_in_one_warning():
    graph = make_graph([
        make_node("P", modify=("a.py",), provides=("a.py::x", "a.py::y")),
        make_node("X", modify=("b.py",), requires=("a.py::x", "a.py::y")),
    ], [make_edge("P", "X", "order")])
    issues = warnings_with(validate(graph), "W4")
    assert len(issues) == 1 and "a.py::x, a.py::y" in issues[0].message


def test_v9_not_reported_for_edges_to_unknown_nodes():
    graph = make_graph([make_node("I", modify=("a.py",))],
                       [make_edge("I", "ghost", "interface")])
    report = validate(graph)
    assert codes(report, "V1") and not codes(report, "V9")


def test_v10_context_file_does_not_count_as_edited():
    graph = make_graph([make_node("A", modify=("a.py",), context_files=("b.py",),
                                  symbols=("b.py::f",))])
    assert codes(validate(graph), "V10")


def test_index_free_rules_still_run_without_index():
    graph = make_graph(
        [make_node("I", modify=("a.py",), symbols=("z.py::f",)),
         make_node("J", modify=("b.py",))],
        [make_edge("I", "J", "interface")],
    )
    assert {issue.code for issue in validate(graph).errors} == {"V9", "V10"}


@pytest.mark.parametrize("size,edges,expected", [
    ("small", [], False),
    ("medium", [("A", "B")], False),
    ("large", [("A", "B")], False),
    ("small", [("A", "B")], True),
])
def test_w1_only_for_small_with_one_successor(size, edges, expected):
    graph = make_graph([make_node("A", modify=("a.py",), size=size),
                        make_node("B", modify=("b.py",))],
                       [make_edge(*pair) for pair in edges])
    assert bool(warnings_with(validate(graph), "W1")) is expected


def test_w3_whitespace_only_final_checks():
    graph = make_graph([make_node("A", modify=("a.py",))], final_checks=("  ",))
    assert warnings_with(validate(graph), "W3")


# ── derive ──

@pytest.mark.xfail(strict=True, reason="建议改 S3: redundant transitive order edge A -> C")
def test_three_nodes_on_one_file_get_a_chain(toy_index):
    graph = make_graph([make_node(n, modify=("runner.py",)) for n in "ABC"])
    derived, entries = derive_edges(graph, toy_index)
    # A->B, B->C; A->C is already implied and must not be added.
    assert edge_tuples(derived) == [("A", "B", "order", "derived"),
                                    ("B", "C", "order", "derived")]
    assert len(entries) == 2
    assert validate(derived, toy_index).ok


def test_two_contract_nodes_keep_list_order(toy_index):
    graph = make_graph([make_node("C2", kind="contract", modify=("models.py",)),
                        make_node("C1", kind="contract", modify=("models.py",))])
    derived, _ = derive_edges(graph, toy_index)
    assert edge_tuples(derived) == [("C2", "C1", "order", "derived")]


def test_derive_appends_to_existing_revision_log_and_is_idempotent(toy_index):
    graph = make_graph([make_node("C", modify=("runner.py",)),
                        make_node("D", modify=("runner.py",))])
    data = graph.model_dump(by_alias=True)
    data["revision_log"] = [{"action": "split", "nodes": ["C", "D"], "reason": "planner split"}]
    graph = Graph.model_validate(data)
    derived, entries = derive_edges(graph, toy_index)
    assert [entry.action for entry in derived.revision_log] == ["split", "add_edge"]
    again, more = derive_edges(derived, toy_index)
    assert more == [] and again.edges == derived.edges


def test_derived_edge_from_contract_satisfies_v9(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("models.py::JobStatus.FAILED",)),
        make_node("C", kind="contract", modify=("models.py",),
                  provides=("models.py::JobStatus.FAILED",)),
        make_node("I", modify=("runner.py",), requires=("tests/test_basic.py::retry_helper",)),
        make_node("P", modify=("tests/test_basic.py",),
                  provides=("tests/test_basic.py::retry_helper",)),
    ])
    derived, _ = derive_edges(graph, toy_index)
    report = validate(derived, toy_index)
    assert report.ok, report.format()
    assert ("C", "X", "interface", "derived") in edge_tuples(derived)
    assert ("P", "I", "full", "derived") in edge_tuples(derived)


def test_multi_provider_entry_dropped_when_dependency_edge_resolves_it(toy_index):
    # X needs a (only P) and b (P and Q). Handling b first finds two
    # providers; then a adds P -> X, which makes P an ancestor and resolves b.
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::b", "runner.py::a")),
        make_node("P", modify=("runner.py",), provides=("runner.py::a", "runner.py::b")),
        make_node("Q", modify=("runner.py",), provides=("runner.py::b",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert validate(derived, toy_index).ok, validate(derived, toy_index).format()
    assert ("P", "X", "full", "derived") in edge_tuples(derived)
    assert "other" not in [entry.action for entry in entries]


def test_multi_provider_entry_dropped_when_order_edge_resolves_it(toy_index):
    # P and X both edit runner.py, so step 2 adds P -> X, which resolves b.
    graph = make_graph([
        make_node("P", modify=("runner.py",), provides=("runner.py::b",)),
        make_node("X", modify=("runner.py",), requires=("runner.py::b",)),
        make_node("Q", modify=("runner.py",), provides=("runner.py::b",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    assert validate(derived, toy_index).ok, validate(derived, toy_index).format()
    assert ("P", "X", "order", "derived") in edge_tuples(derived)
    assert "other" not in [entry.action for entry in entries]


def test_multi_provider_entry_kept_when_still_unresolved(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::b",)),
        make_node("P", modify=("runner.py",), provides=("runner.py::b",)),
        make_node("Q", modify=("runner.py",), provides=("runner.py::b",)),
    ])
    derived, entries = derive_edges(graph, toy_index)
    # P -> Q is ordered (both edit runner.py), but neither reaches X.
    others = [(entry.action, entry.nodes) for entry in entries if entry.action == "other"]
    assert others == [("other", ["X", "P", "Q"])]
    assert codes(validate(derived, toy_index), "V6")
