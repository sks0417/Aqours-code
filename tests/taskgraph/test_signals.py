from __future__ import annotations

import json

import pytest

from aqours_code.taskgraph import dump_graph
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.signals import (
    A_SPLIT_MIN,
    AssumeAllPresent,
    split_signals,
)
from taskgraph_support import make_edge, make_graph, make_node


def report(calls: int, tokens: int = 1000) -> dict:
    return {"totals": {"model_calls": calls, "input_tokens": tokens, "output_tokens": 1}}


def shared_node(node_id: str, *paths: str, **kwargs) -> dict:
    """A node that edits ``paths`` while other nodes edit other parts of them."""
    node = make_node(node_id, modify=paths, **kwargs)
    node["edit_set"]["shared"] = list(paths)
    return node


def two_parallel_on_runner():
    # Both list runner.py in shared, so derive_edges orders neither before the other.
    return make_graph([shared_node("C", "runner.py"), shared_node("D", "runner.py")])


def two_separate():
    return make_graph([make_node("C", modify=("runner.py",)),
                       make_node("D", modify=("store.py",))])


def test_threshold_is_provisional_one():
    assert A_SPLIT_MIN == 1.0


def test_split_when_a_reaches_threshold_and_no_parallel_pair(toy_index):
    result = split_signals(two_separate(), toy_index, report(2, 500))
    assert result["nodes"] == 2
    assert result["model_calls"] == 2 and result["input_tokens"] == 500
    assert result["A"] == pytest.approx(1.0)
    assert result["B"] == 0 and result["pairs"] == []
    assert result["prediction"] == "split"
    assert result["serialise"] == []
    assert result["provisional"] is True


def test_split_after_serialising_lists_the_nodes(toy_index):
    result = split_signals(two_parallel_on_runner(), toy_index, report(4))
    assert result["A"] == pytest.approx(2.0)
    assert result["B"] == 1
    assert result["pairs"] == [{"nodes": ["C", "D"], "files": ["runner.py"]}]
    assert result["files"] == [{"path": "runner.py", "node_count": 2, "nodes": ["C", "D"]}]
    assert result["prediction"] == "split-after-serialising"
    assert result["serialise"] == ["C", "D"]


def test_single_when_a_below_threshold_and_parallel_pair(toy_index):
    result = split_signals(two_parallel_on_runner(), toy_index, report(1))
    assert result["A"] == pytest.approx(0.5)
    assert result["B"] == 1
    assert result["prediction"] == "single"
    assert result["serialise"] == []


def test_undetermined_when_a_below_threshold_and_no_parallel_pair(toy_index):
    result = split_signals(two_separate(), toy_index, report(1))
    assert result["A"] == pytest.approx(0.5)
    assert result["B"] == 0
    assert result["prediction"] == "undetermined"


def test_explicit_and_indirect_ancestors_are_not_counted(toy_index):
    graph = make_graph(
        [shared_node("X", "runner.py"), shared_node("Y", "store.py"),
         shared_node("Z", "runner.py", "store.py")],
        [make_edge("X", "Y"), make_edge("Y", "Z")],
    )
    result = split_signals(graph, toy_index, report(9))
    assert result["B"] == 0
    assert result["prediction"] == "split"


def test_edges_from_derive_edges_make_ancestors(toy_index):
    # Both modify runner.py without sharing it: derive_edges adds C -> D.
    graph = make_graph([make_node("C", modify=("runner.py",)),
                        make_node("D", modify=("runner.py",)),
                        shared_node("E", "models.py"),
                        shared_node("F", "models.py")])
    result = split_signals(graph, toy_index, report(1))
    assert result["edges_in_graph"] == 0
    assert result["edges_added_by_derive"] == 1
    assert result["pairs"] == [{"nodes": ["E", "F"], "files": ["models.py"]}]
    assert result["B"] == 1


def test_requires_edge_from_derive_edges_makes_ancestor(toy_index):
    # runner.py::purge_jobs is new, so derive_edges orders its provider first.
    graph = make_graph([
        shared_node("X", "runner.py", requires=("runner.py::purge_jobs",)),
        shared_node("P", "runner.py", provides=("runner.py::purge_jobs",)),
    ])
    assert split_signals(graph, toy_index, report(1))["B"] == 0
    # Treated as present at the base commit, the symbol needs no provider.
    assert split_signals(graph, AssumeAllPresent(commit="HEAD"), report(1))["B"] == 1


def test_test_files_and_non_py_files_are_not_counted(toy_index):
    graph = make_graph([shared_node("C", "tests/test_basic.py", "README.md"),
                        shared_node("D", "tests/test_basic.py", "README.md")])
    result = split_signals(graph, toy_index, report(1))
    assert result["B"] == 0
    assert result["prediction"] == "undetermined"
    assert result["non_py_shared"]["pairs"] == [{"nodes": ["C", "D"], "files": ["README.md"]}]
    assert result["non_py_shared"]["files"] == [
        {"path": "README.md", "node_count": 2, "nodes": ["C", "D"]}]


def test_file_summary_counts_nodes_per_file(toy_index):
    graph = make_graph([shared_node("A", "runner.py", "models.py"),
                        shared_node("B", "runner.py", "models.py"),
                        shared_node("C", "runner.py")])
    result = split_signals(graph, toy_index, report(1))
    assert result["B"] == 3
    assert result["files"] == [
        {"path": "runner.py", "node_count": 3, "nodes": ["A", "B", "C"]},
        {"path": "models.py", "node_count": 2, "nodes": ["A", "B"]},
    ]


def test_missing_totals_make_a_unknown(toy_index):
    result = split_signals(two_parallel_on_runner(), toy_index, {"planner": "v3"})
    assert result["A"] == "unknown"
    assert result["model_calls"] == "unknown"
    assert result["prediction"] == "unknown"
    assert result["B"] == 1


def test_cli_without_report_prints_unknown(tmp_path, toy_repo, capsys):
    graph_path = tmp_path / "graph.json"
    dump_graph(two_parallel_on_runner(), graph_path)
    code = main(["signals", str(graph_path), "--repo", str(toy_repo.path), "--json"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["report_note"] == "not found"
    assert result["index"]["built"] is True
    assert result["A"] == "unknown" and result["prediction"] == "unknown"
    assert result["B"] == 1 and result["provisional"] is True


def test_cli_reads_report_next_to_graph(tmp_path, toy_repo, capsys):
    graph_path = tmp_path / "graph.json"
    dump_graph(two_parallel_on_runner(), graph_path)
    (tmp_path / "graph.json.report.json").write_text(json.dumps(report(4, 123)),
                                                     encoding="utf-8")
    assert main(["signals", str(graph_path), "--repo", str(toy_repo.path)]) == 0
    text = capsys.readouterr().out
    assert "planning: 4 model calls, 123 input tokens" in text
    assert "A = planning calls / nodes = 4 / 2 = 2.00" in text
    assert "B = 1 node pairs" in text
    assert "C + D: runner.py" in text
    assert "prediction (provisional, A_SPLIT_MIN = 1.0): split-after-serialising" in text
    assert "serialise: C, D" in text


def test_cli_without_repository_still_computes_b(tmp_path, capsys):
    graph_path = tmp_path / "graph.json"
    dump_graph(two_parallel_on_runner(), graph_path)  # repo "toy" does not exist here
    assert main(["signals", str(graph_path), "--repo", str(tmp_path / "missing"),
                 "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["index"]["built"] is False
    assert "treated as present" in result["index"]["note"]
    assert result["B"] == 1
