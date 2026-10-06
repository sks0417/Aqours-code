"""compare: structural figures of task graphs and node matching."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aqours_code.taskgraph import load_graph
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.compare import (
    compare_graphs,
    critical_path_length,
    format_comparison,
    graph_stats,
    jaccard,
    match_nodes,
    max_parallel_width,
)
from taskgraph_support import make_edge, make_graph, make_node

EXPERIMENTS = Path(__file__).resolve().parents[2] / "experiments" / "taskgraph"
HANDWRITTEN = [(task, variant) for task in ("job_platform", "job_runner")
               for variant in ("coupled", "modular")]


def small_graph():
    """A -> B -> D, A -> C, A -> E, plus a shortcut A -> D; D only writes tests."""
    return make_graph([
        make_node("A", kind="contract", modify=("models.py", "store.py")),
        make_node("B", modify=("runner.py",), create=("tests/test_b.py",)),
        make_node("C", modify=("store.py",)),
        make_node("D", create=("tests/test_all.py",), modify=("tests/test_basic.py",)),
        make_node("E", modify=("README.md",)),
    ], [make_edge("A", "B", "interface"), make_edge("A", "C", "interface"),
        make_edge("A", "E", "interface"), make_edge("B", "D"), make_edge("A", "D")])


def test_critical_path_width_and_test_only_nodes():
    graph = small_graph()
    assert critical_path_length(graph) == 3          # A -> B -> D
    assert max_parallel_width(graph) == 3            # B, C, E share layer 2
    stats = graph_stats(graph, None)
    assert stats["test_only_nodes"] == ["D"]         # B also writes code
    assert (stats["nodes"], stats["contract_nodes"], stats["implement_nodes"]) == (5, 1, 4)
    assert stats["contract_files"] == ["models.py", "store.py"]


def test_independent_nodes_and_cycles():
    flat = make_graph([make_node("X", modify=("a.py",)), make_node("Y", modify=("b.py",))])
    assert (critical_path_length(flat), max_parallel_width(flat)) == (1, 2)
    cyclic = make_graph([make_node("X", modify=("a.py",)), make_node("Y", modify=("b.py",))],
                        [make_edge("X", "Y"), make_edge("Y", "X")])
    assert critical_path_length(cyclic) is None and max_parallel_width(cyclic) is None
    assert graph_stats(cyclic, None)["error_codes"] == {"V2": 1}


def test_match_nodes_ignores_test_files():
    planner = make_graph([
        make_node("P1", modify=("models.py", "store.py", "runner.py")),
        make_node("P2", modify=("README.md",), create=("tests/test_x.py",)),
    ])
    matches = match_nodes(planner, small_graph())
    by_node = {match["handwritten"]: (match["planner"], match["similarity"])
               for match in matches}
    assert by_node["A"] == ("P1", pytest.approx(2 / 3, abs=1e-4))
    assert by_node["B"] == ("P1", pytest.approx(1 / 3, abs=1e-4))
    assert by_node["E"] == ("P2", 1.0)
    assert by_node["D"] == (None, 0.0)  # no code files: shares no file, no match
    assert jaccard(set(), set()) == 1.0


def test_zero_similarity_shows_no_match(toy_repo):
    commit = {"base_commit": toy_repo.commit}
    planner = make_graph([make_node("P1", modify=("models.py",))]).model_copy(update=commit)
    handwritten = make_graph([make_node("H1", modify=("models.py",)),
                              make_node("H2", modify=("runner.py",))]).model_copy(update=commit)
    result = compare_graphs(planner, handwritten, toy_repo.path)
    assert result["matches"][1] == {"handwritten": "H2", "planner": None, "similarity": 0.0}
    assert result["mean_similarity"] == 0.5  # the unmatched node counts as 0
    match_row = next(line for line in format_comparison(result).splitlines()
                     if line.startswith("| Node match"))
    assert "H1 -> P1 (1.00)<br>H2: no match<br>mean 0.50" in match_row
    assert "(0.00)" not in match_row


@pytest.mark.parametrize(("task", "variant"), HANDWRITTEN)
def test_handwritten_graph_matches_itself(task, variant, tmp_path):
    repo = tmp_path / "repo"
    subprocess.run([sys.executable, str(EXPERIMENTS / task / "make_repo.py"), variant,
                    str(repo)], check=True, capture_output=True, timeout=60)
    path = EXPERIMENTS / task / variant / "graphs" / "handwritten.json"
    graph = load_graph(path)
    result = compare_graphs(graph, graph, repo)
    assert result["notes"] == []
    assert [match["similarity"] for match in result["matches"]] == [1.0] * len(graph.nodes)
    assert result["mean_similarity"] == 1.0
    assert result["planner"] == result["handwritten"]
    assert result["planner"]["errors"] == 0
    assert result["planner"]["test_only_nodes"] == []
    # The planner-only checks P1-P3 are not part of the general validation.
    assert not {"P1", "P2", "P3"} & set(result["planner"]["error_codes"])


def test_format_and_cli(toy_repo, tmp_path, capsys):
    graph = small_graph().model_copy(update={"base_commit": toy_repo.commit})
    text = format_comparison(compare_graphs(graph, graph, toy_repo.path), "p.json", "h.json")
    lines = text.splitlines()
    assert lines[0] == "| | p.json | h.json |"
    assert "| Critical path | 3 | 3 |" in lines
    assert "| Max parallel width | 3 | 3 |" in lines
    assert "| Test-only nodes | D | D |" in lines
    assert any(line.startswith("| Node match") and "A -> A (1.00)" in line
               and "mean 1.00" in line for line in lines)

    path = tmp_path / "graph.json"
    path.write_text(graph.model_dump_json(by_alias=True), encoding="utf-8")
    json_out = tmp_path / "compare.json"
    code = main(["compare", str(path), str(path), "--repo", str(toy_repo.path),
                 "--json", str(json_out)])
    assert code == 0
    assert "| Nodes | 5 (contract 1, implement 4) |" in capsys.readouterr().out
    assert json_out.is_file()
