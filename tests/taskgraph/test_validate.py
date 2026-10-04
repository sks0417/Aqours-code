from __future__ import annotations

from aqours_code.taskgraph import derive_edges, load_graph, validate
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.validate import Issue
from taskgraph_support import EXAMPLE_GRAPH, make_edge, make_graph, make_node


def errors_with(report, code):
    return [issue for issue in report.errors if issue.code == code]


# ── example and single-node graphs ──

def test_example_graph_passes(toy_index):
    report = validate(load_graph(EXAMPLE_GRAPH), toy_index)
    assert report.ok, report.format()
    assert report.warnings == []


def test_single_node_graph_passes(toy_index):
    graph = make_graph([make_node("only", modify=("runner.py",),
                                  requires=("runner.py::run_loop",))])
    report = validate(graph, toy_index)
    assert report.ok, report.format()


def test_single_node_graph_still_checks_files_and_commands(toy_index):
    graph = make_graph([make_node("only", modify=("missing.py",), commands=())])
    report = validate(graph, toy_index)
    assert report.codes() == ["V3", "V4"]


def test_without_index_v3_and_v6_are_skipped_with_warnings():
    graph = make_graph([make_node("only", modify=("missing.py",),
                                  requires=("nowhere.py::x",))])
    report = validate(graph)
    assert report.ok
    skipped = [issue for issue in report.warnings if issue.code in {"V3", "V6"}]
    assert [issue.code for issue in skipped] == ["V3", "V6"]
    assert all("skipped" in issue.message for issue in skipped)


# ── V1 structure ──

def test_v1_passes_for_distinct_edges(toy_index):
    graph = make_graph(
        [make_node("A", modify=("models.py",)), make_node("B", modify=("store.py",))],
        [make_edge("A", "B", "full"), make_edge("A", "B", "order")],
    )
    assert not errors_with(validate(graph, toy_index), "V1")


def test_v1_duplicate_node_id():
    graph = make_graph([make_node("A", modify=("a.py",)), make_node("A", modify=("b.py",))])
    issues = errors_with(validate(graph), "V1")
    assert len(issues) == 1 and issues[0].nodes == ["A"]


def test_v1_unknown_node_and_self_loop_and_duplicate_edge():
    graph = make_graph(
        [make_node("A", modify=("a.py",)), make_node("B", modify=("b.py",))],
        [make_edge("A", "ghost"), make_edge("B", "B"),
         make_edge("A", "B"), make_edge("A", "B")],
    )
    messages = [issue.message for issue in errors_with(validate(graph), "V1")]
    assert len(messages) == 3
    assert "unknown node(s): ghost" in messages[0]
    assert "self-loop" in messages[1]
    assert "duplicate full edge A -> B" in messages[2]


# ── V2 acyclic ──

def test_v2_passes_for_dag():
    graph = make_graph(
        [make_node(n, modify=(f"{n}.py",)) for n in "ABC"],
        [make_edge("A", "B"), make_edge("B", "C"), make_edge("A", "C", "order")],
    )
    assert not errors_with(validate(graph), "V2")


def test_v2_reports_cycle_nodes():
    graph = make_graph(
        [make_node(n, modify=(f"{n}.py",)) for n in "ABCD"],
        [make_edge("A", "B"), make_edge("B", "C", "order"),
         make_edge("C", "A", "interface"), make_edge("C", "D")],
    )
    issues = errors_with(validate(graph), "V2")
    assert len(issues) == 1
    assert issues[0].nodes == ["A", "B", "C"]
    assert "A -> B -> C -> A" in issues[0].message


# ── V3 file existence ──

def test_v3_passes(toy_index):
    graph = make_graph([
        make_node("A", modify=("store.py",), create=("new_mod.py",),
                  context_files=("README.md",)),
        make_node("B", create=("other.py",), context_files=("new_mod.py",)),
    ], [make_edge("A", "B")])
    assert not errors_with(validate(graph, toy_index), "V3")


def test_v3_failures(toy_index):
    graph = make_graph([
        make_node("A", modify=("missing.py",), create=("runner.py",),
                  context_files=("nowhere.md",)),
    ])
    messages = [issue.message for issue in errors_with(validate(graph, toy_index), "V3")]
    assert len(messages) == 3
    assert "modifies missing.py" in messages[0]
    assert "creates runner.py" in messages[1]
    assert "context file nowhere.md" in messages[2]


def test_v3_modify_file_created_by_ancestor(toy_index):
    graph = make_graph([
        make_node("A", create=("util.py",)),
        make_node("B", modify=("util.py", "store.py")),
    ], [make_edge("A", "B")])
    report = validate(graph, toy_index)
    assert report.ok, report.format()


def test_v3_modify_file_created_by_transitive_ancestor(toy_index):
    graph = make_graph([
        make_node("A", create=("util.py",)),
        make_node("M", modify=("store.py",)),
        make_node("B", modify=("util.py",)),
    ], [make_edge("A", "M", "order"), make_edge("M", "B", "order")])
    assert not errors_with(validate(graph, toy_index), "V3")


def test_v3_modify_file_created_by_non_ancestor(toy_index):
    graph = make_graph([
        make_node("A", create=("util.py",)),
        make_node("B", modify=("util.py",)),
    ])
    issues = errors_with(validate(graph, toy_index), "V3")
    assert len(issues) == 1
    assert issues[0].nodes == ["B", "A"]
    assert issues[0].message == (
        "modifies util.py, which is created by A, but A is not an ancestor of B "
        "(missing edge?)")


def test_v3_modify_file_created_by_descendant(toy_index):
    graph = make_graph([
        make_node("B", modify=("util.py",)),
        make_node("A", create=("util.py",)),
    ], [make_edge("B", "A")])
    issues = errors_with(validate(graph, toy_index), "V3")
    assert [issue.nodes for issue in issues] == [["B", "A"]]


# ── V11 single creator ──

def test_v11_passes_for_distinct_new_files():
    graph = make_graph([make_node("A", create=("a_new.py",)),
                        make_node("B", create=("b_new.py",))])
    assert not errors_with(validate(graph), "V11")


def test_v11_same_file_created_twice_without_edge_and_without_index():
    graph = make_graph([
        make_node("A", create=("util.py",)),
        make_node("B", modify=("store.py",)),
        make_node("C", create=("util.py", "other.py")),
        make_node("D", create=("util.py",)),
    ])
    issues = errors_with(validate(graph), "V11")
    assert len(issues) == 1
    assert issues[0].nodes == ["A", "C", "D"]
    assert "util.py" in issues[0].message


def test_v11_same_file_created_twice_with_edge(toy_index):
    graph = make_graph([
        make_node("A", create=("util.py",)),
        make_node("B", create=("util.py",)),
    ], [make_edge("A", "B")])
    issues = errors_with(validate(graph, toy_index), "V11")
    assert [issue.nodes for issue in issues] == [["A", "B"]]


# ── V4 check commands ──

def test_v4_failures():
    graph = make_graph([
        make_node("A", modify=("a.py",), commands=()),
        make_node("B", modify=("b.py",), commands=("", "   ")),
        make_node("C", modify=("c.py",), commands=("", "pytest")),
    ])
    assert [issue.nodes for issue in errors_with(validate(graph), "V4")] == [["A"], ["B"]]


# ── V5 edit conflicts ──

def test_v5_passes_when_one_is_ancestor_via_any_edge_type():
    graph = make_graph(
        [make_node("A", modify=("runner.py",)), make_node("B", modify=("x.py",)),
         make_node("C", modify=("runner.py",))],
        [make_edge("A", "B", "order"), make_edge("B", "C", "interface")],
    )
    assert not errors_with(validate(graph), "V5")


def test_v5_reports_overlapping_files():
    graph = make_graph([
        make_node("A", modify=("models.py",)),
        make_node("C", modify=("runner.py", "store.py")),
        make_node("D", modify=("runner.py",), create=("store.py",)),
    ])
    issues = errors_with(validate(graph), "V5")
    assert len(issues) == 1
    assert issues[0].format() == (
        "[V5] C, D: both edit runner.py, store.py but neither is an ancestor "
        "of the other")


# ── V6 required symbols ──

def test_v6_passes_from_repo_and_ancestor(toy_index):
    graph = make_graph([
        make_node("A", kind="contract", modify=("models.py",),
                  provides=("models.py::JobStatus.FAILED",)),
        make_node("B", modify=("store.py",)),
        make_node("C", modify=("runner.py",),
                  requires=("runner.py::run_loop", "models.py::JobStatus.FAILED")),
    ], [make_edge("A", "B", "interface"), make_edge("B", "C", "order")])
    assert not errors_with(validate(graph, toy_index), "V6")


def test_v6_provider_not_ancestor(toy_index):
    graph = make_graph([
        make_node("P", modify=("models.py",), provides=("models.py::JobStatus.FAILED",)),
        make_node("X", modify=("store.py",), requires=("models.py::JobStatus.FAILED",)),
    ])
    issues = errors_with(validate(graph, toy_index), "V6")
    assert len(issues) == 1
    assert issues[0].nodes == ["X", "P"]
    assert "provided by P" in issues[0].message and "missing dependency edge" in issues[0].message


def test_v6_no_source(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("store.py::JobStore.purge",)),
    ])
    issues = errors_with(validate(graph, toy_index), "V6")
    assert len(issues) == 1 and issues[0].nodes == ["X"]
    assert "neither defined at the base commit nor provided" in issues[0].message


def test_v6_self_provided_symbol_is_not_a_source(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("store.py::JobStore.purge",),
                  provides=("store.py::JobStore.purge",)),
    ])
    assert len(errors_with(validate(graph, toy_index), "V6")) == 1


# ── V7 non-empty edit set ──

def test_v7():
    graph = make_graph([make_node("A", modify=("a.py",)), make_node("B")])
    assert [issue.nodes for issue in errors_with(validate(graph), "V7")] == [["B"]]


# ── V8 symbol format ──

def test_v8_passes_for_schema_validated_graph():
    graph = make_graph([make_node("A", modify=("a.py",), requires=("a.py::x",),
                                  symbols=("a.py::Klass.method",))])
    assert not errors_with(validate(graph), "V8")


def test_v8_catches_symbols_that_bypassed_the_schema():
    graph = make_graph([make_node("A", modify=("a.py",))])
    graph.nodes[0].requires.append("a.py:x")
    graph.nodes[0].provides.append("a.py::1x")
    graph.nodes[0].edit_set.symbols.append("../a.py::x")
    issues = errors_with(validate(graph), "V8")
    assert len(issues) == 3
    assert "requires" in issues[0].message
    assert "provides" in issues[1].message
    assert "edit_set.symbols" in issues[2].message


# ── V9 interface edges start at contract nodes ──

def test_v9_passes_for_interface_from_contract_and_other_types_from_implement():
    graph = make_graph(
        [make_node("C", kind="contract", modify=("models.py",)),
         make_node("I", modify=("store.py",)), make_node("J", modify=("runner.py",))],
        [make_edge("C", "I", "interface"), make_edge("I", "J", "full"),
         make_edge("C", "J", "order")],
    )
    assert not errors_with(validate(graph), "V9")


def test_v9_interface_edge_from_implement_node():
    graph = make_graph(
        [make_node("I", modify=("store.py",)), make_node("J", modify=("runner.py",))],
        [make_edge("I", "J", "interface")],
    )
    issues = errors_with(validate(graph), "V9")
    assert len(issues) == 1 and issues[0].nodes == ["I", "J"]
    assert "must start at a contract node" in issues[0].message


# ── V10 edit_set.symbols belong to edited files ──

def test_v10_passes_for_symbols_in_modified_or_created_files():
    graph = make_graph([make_node(
        "A", modify=("store.py",), create=("new_mod.py",),
        symbols=("store.py::JobStore.add", "new_mod.py::helper"))])
    assert not errors_with(validate(graph), "V10")


def test_v10_symbol_outside_edit_set():
    graph = make_graph([make_node(
        "A", modify=("store.py",), symbols=("store.py::JobStore.add", "runner.py::run_loop"))])
    issues = errors_with(validate(graph), "V10")
    assert len(issues) == 1 and issues[0].nodes == ["A"]
    assert "runner.py::run_loop" in issues[0].message


def test_v10_leaves_malformed_symbols_to_v8():
    graph = make_graph([make_node("A", modify=("store.py",))])
    graph.nodes[0].edit_set.symbols.append("store.py:add")
    report = validate(graph)
    assert errors_with(report, "V8") and not errors_with(report, "V10")


def test_v10_provides_outside_edit_set_is_reported_even_after_derive(toy_index):
    graph = make_graph([
        make_node("A", modify=("runner.py",), provides=("store.py::new_fn",)),
        make_node("B", modify=("models.py",), requires=("store.py::new_fn",)),
    ])
    derived, _ = derive_edges(graph, toy_index)
    assert ("A", "B", "full") in [(e.from_, e.to, e.type) for e in derived.edges]
    issues = errors_with(validate(derived, toy_index), "V10")
    assert len(issues) == 1 and issues[0].nodes == ["A"]
    assert issues[0].message == (
        "provides store.py::new_fn, but store.py is not in edit_set.modify or "
        "edit_set.create")


def test_v10_provides_in_created_or_modified_file_passes(toy_index):
    graph = make_graph([
        make_node("A", create=("helpers.py",), modify=("runner.py",),
                  provides=("helpers.py::new_fn", "runner.py::run_loop")),
        make_node("B", modify=("models.py",),
                  requires=("helpers.py::new_fn", "runner.py::run_loop")),
    ], [make_edge("A", "B")])
    report = validate(graph, toy_index)
    assert report.ok, report.format()


def test_v10_checks_provides_without_index():
    graph = make_graph([
        make_node("A", modify=("a.py",), provides=("b.py::f",), symbols=("c.py::g",)),
    ])
    messages = [issue.message for issue in errors_with(validate(graph), "V10")]
    assert len(messages) == 2
    assert messages[0].startswith("edit_set.symbols lists c.py::g")
    assert messages[1].startswith("provides b.py::f")


def test_v10_leaves_malformed_provides_to_v8():
    graph = make_graph([make_node("A", modify=("a.py",))])
    graph.nodes[0].provides.append("b.py:f")
    report = validate(graph)
    assert errors_with(report, "V8") and not errors_with(report, "V10")


# ── warnings ──

def test_w1_small_node_with_single_downstream():
    graph = make_graph(
        [make_node("A", modify=("a.py",), size="small"), make_node("B", modify=("b.py",)),
         make_node("S", modify=("s.py",), size="small"), make_node("T", modify=("t.py",)),
         make_node("U", modify=("u.py",))],
        [make_edge("A", "B"), make_edge("A", "B", "order"),
         make_edge("S", "T"), make_edge("S", "U")],
    )
    issues = [issue for issue in validate(graph).warnings if issue.code == "W1"]
    assert [issue.nodes for issue in issues] == [["A", "B"]]


def test_w2_unused_provides():
    graph = make_graph([
        make_node("A", modify=("a.py",), provides=("a.py::used", "a.py::unused")),
        make_node("B", modify=("b.py",), requires=("a.py::used",)),
    ], [make_edge("A", "B")])
    issues = [issue for issue in validate(graph).warnings if issue.code == "W2"]
    assert len(issues) == 1 and "a.py::unused" in issues[0].message


def test_w3_missing_final_checks():
    graph = make_graph([make_node("A", modify=("a.py",))], final_checks=())
    report = validate(graph)
    assert report.ok
    assert "W3" in report.warning_codes()


def test_w4_order_edge_carrying_a_required_symbol():
    graph = make_graph([
        make_node("P", modify=("store.py",), provides=("store.py::JobStore.purge",)),
        make_node("X", modify=("runner.py",), requires=("store.py::JobStore.purge",)),
    ], [make_edge("P", "X", "order")])
    report = validate(graph)
    assert report.ok
    issues = [issue for issue in report.warnings if issue.code == "W4"]
    assert len(issues) == 1 and issues[0].nodes == ["P", "X"]
    assert "store.py::JobStore.purge" in issues[0].message
    assert "use interface or full" in issues[0].message


def test_w4_not_raised_for_full_or_mixed_edges_or_unrelated_order_edges():
    graph = make_graph([
        make_node("P", modify=("store.py",), provides=("store.py::JobStore.purge",)),
        make_node("X", modify=("runner.py",), requires=("store.py::JobStore.purge",)),
        make_node("Y", modify=("models.py",), requires=("store.py::JobStore.purge",)),
        make_node("Z", modify=("README.md",)),
    ], [make_edge("P", "X", "full"),
        make_edge("P", "Y", "order"), make_edge("P", "Y", "full"),
        make_edge("X", "Z", "order")])
    assert "W4" not in validate(graph).warning_codes()


def test_w5_existing_symbol_changed_by_non_ancestor(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::run_loop",)),
        make_node("P", modify=("runner.py",), provides=("runner.py::run_loop",)),
        make_node("Q", modify=("runner.py",), provides=("runner.py::run_loop",)),
    ], [make_edge("P", "Q", "order")])  # P and Q both edit runner.py (V5)
    report = validate(graph, toy_index)
    assert report.ok
    issues = [issue for issue in report.warnings if issue.code == "W5"]
    assert len(issues) == 1
    assert issues[0].nodes == ["X", "P", "Q"]
    assert ("runner.py::run_loop exists at the base commit but is changed by P, Q "
            "which are not ancestors of X") in issues[0].message


def test_w5_not_raised_when_changer_is_ancestor(toy_index):
    graph = make_graph([
        make_node("P", modify=("runner.py",), provides=("runner.py::run_loop",)),
        make_node("X", modify=("store.py",), requires=("runner.py::run_loop",)),
    ], [make_edge("P", "X")])
    assert "W5" not in validate(graph, toy_index).warning_codes()


def test_w5_not_raised_for_symbols_missing_from_repo(toy_index):
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::purge_jobs",)),
        make_node("P", modify=("runner.py",), provides=("runner.py::purge_jobs",)),
    ])
    report = validate(graph, toy_index)
    assert "W5" not in report.warning_codes()
    assert "V6" in report.codes()


def test_w5_skipped_without_index():
    graph = make_graph([
        make_node("X", modify=("store.py",), requires=("runner.py::run_loop",)),
        make_node("P", modify=("runner.py",), provides=("runner.py::run_loop",)),
    ])
    skipped = [issue for issue in validate(graph).warnings if issue.code == "W5"]
    assert len(skipped) == 1 and "skipped" in skipped[0].message


# ── report and CLI ──

def test_issue_format_without_nodes():
    assert Issue("W3", [], "final_checks is empty").format() == "[W3] final_checks is empty"


def _single_error_line(capsys) -> str:
    lines = capsys.readouterr().err.strip().splitlines()
    errors = [line for line in lines if not line.startswith("index warning: ")]
    assert len(errors) == 1 and errors[0].startswith("error: ")
    return errors[0]


def test_cli_unreadable_graph_path_exits_2(tmp_path, capsys):
    assert main(["validate", str(tmp_path)]) == 2
    assert str(tmp_path) in _single_error_line(capsys)


def test_cli_non_utf8_graph_exits_2(tmp_path, capsys):
    graph = tmp_path / "graph.json"
    graph.write_bytes(b"\xff\xfe\x00{")
    assert main(["validate", str(graph)]) == 2
    _single_error_line(capsys)


def test_cli_unwritable_out_exits_2(tmp_path, toy_repo, capsys):
    from aqours_code.taskgraph import dump_graph

    graph = make_graph([make_node("A", modify=("runner.py",))]).model_copy(
        update={"base_commit": toy_repo.commit})
    source = tmp_path / "graph.json"
    dump_graph(graph, source)
    out_dir = tmp_path / "out_is_a_directory"
    out_dir.mkdir()
    assert main(["derive", str(source), "--repo", str(toy_repo.path),
                 "--out", str(out_dir)]) == 2
    assert "cannot write" in _single_error_line(capsys)
    assert main(["export-schema", "--out", str(out_dir)]) == 2
    assert "cannot write" in _single_error_line(capsys)


def test_cli_missing_git_executable_exits_2(tmp_path, monkeypatch, capsys):
    import subprocess

    from aqours_code.taskgraph import repo_index

    def missing_git(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(repo_index.subprocess, "run", missing_git)
    assert repo_index.subprocess is subprocess
    assert main(["index", "--repo", str(tmp_path), "--commit", "HEAD"]) == 2
    assert "cannot run git" in _single_error_line(capsys)


def test_cli_unknown_commit_names_the_commit(toy_repo, capsys):
    assert main(["index", "--repo", str(toy_repo.path), "--commit", "no-such-ref"]) == 2
    line = _single_error_line(capsys)
    assert "cannot resolve commit 'no-such-ref'" in line
    assert not line.rstrip().endswith("failed:")


def test_cli_validate_exit_codes(tmp_path, toy_repo, capsys):
    good = tmp_path / "good.json"
    good.write_text(EXAMPLE_GRAPH.read_text(encoding="utf-8").replace(
        "48e0476cbc3aac7a8ed14cde30fde8165053b4b5", toy_repo.commit), encoding="utf-8")
    assert main(["validate", str(good), "--repo", str(toy_repo.path)]) == 0
    assert "errors (0):" in capsys.readouterr().out

    bad = tmp_path / "bad.json"
    graph = make_graph([make_node("C", modify=("runner.py",)),
                        make_node("D", modify=("runner.py",))])
    from aqours_code.taskgraph import dump_graph
    dump_graph(graph, bad)
    assert main(["validate", str(bad)]) == 1
    out = capsys.readouterr().out
    assert ("[V5] C, D: both edit runner.py but neither is an ancestor of the other"
            in out.splitlines())

    broken = tmp_path / "broken.json"
    broken.write_text('{"request_id": 1}', encoding="utf-8")
    assert main(["validate", str(broken)]) == 2
