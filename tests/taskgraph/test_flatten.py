"""Tests for flatten_graph and the committed single_planned.json graphs."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aqours_code.taskgraph import build_index, flatten_graph, load_graph, validate
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.flatten import PLANNED_ID, STEPS_INTRO, topological_order
from taskgraph_support import EXAMPLE_GRAPH, make_edge, make_graph, make_node

EXPERIMENTS = Path(__file__).resolve().parents[2] / "experiments" / "taskgraph"
EXPERIMENT_GRAPHS = [(task, variant) for task in ("job_runner", "job_platform")
                     for variant in ("coupled", "modular")]


def assert_flattened(original, flat) -> None:
    """The checks every flattened graph must pass, whatever its source."""
    assert len(flat.nodes) == 1 and flat.edges == []
    node = flat.nodes[0]
    assert (node.id, node.kind) == (PLANNED_ID, "implement")
    order = topological_order(original)
    positions = [node.goal.index(f"{step.title} [{step.id}]") for step in order]
    assert positions == sorted(positions)
    assert node.goal.startswith(original.request.strip())
    assert STEPS_INTRO in node.goal
    modify = {p for n in original.nodes for p in n.edit_set.modify}
    create = {p for n in original.nodes for p in n.edit_set.create}
    assert set(node.edit_set.create) == create
    assert set(node.edit_set.modify) == modify - create
    assert not set(node.edit_set.modify) & set(node.edit_set.create)
    assert set(node.edit_set.symbols) == {s for n in original.nodes for s in n.edit_set.symbols}
    assert set(node.provides) == {s for n in original.nodes for s in n.provides}
    commands = [c for n in order for c in n.check.commands]
    assert node.check.commands == list(dict.fromkeys(commands))
    assert node.check.timeout_s == max(n.check.timeout_s for n in original.nodes)
    for field in ("request_id", "request", "repo", "base_commit", "final_checks", "generator"):
        assert getattr(flat, field) == getattr(original, field)
    assert flat.revision_log[:-1] == original.revision_log
    last = flat.revision_log[-1]
    assert last.action == "other" and original.request_id in last.reason
    assert last.nodes == [n.id for n in order]


def test_flatten_toy_example(toy_index):
    original = load_graph(EXAMPLE_GRAPH)
    flat = flatten_graph(original)
    assert_flattened(original, flat)
    report = validate(flat, toy_index)
    assert report.ok, report.format()
    node = flat.nodes[0]
    assert node.requires == ["store.py::JobStore.jobs", "runner.py::MAX_ATTEMPTS"]
    assert node.requires_impl == []  # store.py::JobStore.mark_failed comes from a node
    assert "Step 1 of 4: Add FAILED status [status-failed]" in node.goal


def test_topological_order_breaks_ties_by_node_order():
    graph = make_graph([make_node("C", modify=("c.py",)), make_node("B", modify=("b.py",)),
                        make_node("A", modify=("a.py",))], [make_edge("A", "B")])
    assert [node.id for node in topological_order(graph)] == ["C", "A", "B"]


def test_flatten_merges_files_symbols_checks_and_context():
    make = make_node("make", create=("pkg/new.py", "tests/test_new.py"), modify=("pkg/a.py",),
                     provides=("pkg/new.py::helper",), commands=("check-a", "check-shared"),
                     context_files=("pkg/a.py",), size="small")
    use = make_node("use", modify=("pkg/new.py", "pkg/a.py", "pkg/b.py"),
                    requires=("pkg/new.py::helper", "pkg/a.py::base"),
                    requires_impl=("pkg/b.py::other", "pkg/new.py::helper2"),
                    symbols=("pkg/new.py::helper2",), commands=("check-shared", "check-b"),
                    context_files=("pkg/new.py", "pkg/b.py"), size="large")
    use["check"]["timeout_s"] = 900
    graph = make_graph([make, use], [make_edge("make", "use")])
    flat = flatten_graph(graph)
    assert_flattened(graph, flat)
    node = flat.nodes[0]
    assert node.edit_set.modify == ["pkg/a.py", "pkg/b.py"]
    assert node.edit_set.create == ["pkg/new.py", "tests/test_new.py"]
    assert node.requires == ["pkg/a.py::base"]
    assert node.requires_impl == ["pkg/b.py::other"]
    assert node.check.commands == ["check-a", "check-shared", "check-b"]
    assert node.check.timeout_s == 900
    assert node.context_files == ["pkg/a.py", "pkg/b.py"]
    assert node.size == "large"
    step = node.goal.split("\n\n")[2]
    assert step.splitlines() == ["Step 1 of 2: make [make]", "do make", "Modify: pkg/a.py",
                                 "Create: pkg/new.py", "Tests to write: tests/test_new.py"]


def test_flatten_rejects_a_cycle():
    graph = make_graph([make_node("A", modify=("a.py",)), make_node("B", modify=("b.py",))],
                       [make_edge("A", "B"), make_edge("B", "A")])
    with pytest.raises(ValueError, match="cycle"):
        flatten_graph(graph)


def test_cli_flatten_writes_a_valid_graph(tmp_path, toy_repo, capsys):
    out = tmp_path / "planned.json"
    assert main(["flatten", str(EXAMPLE_GRAPH), "--out", str(out),
                 "--repo", str(toy_repo.path)]) == 0
    assert load_graph(out) == flatten_graph(load_graph(EXAMPLE_GRAPH))
    assert "errors (0)" in capsys.readouterr().out


@pytest.fixture(scope="module")
def experiment_repos(tmp_path_factory):
    """Base repositories of the four experiment graphs, built once."""
    root = tmp_path_factory.mktemp("flatten")
    repos = {}
    for task, variant in EXPERIMENT_GRAPHS:
        dest = root / f"{task}_{variant}"
        proc = subprocess.run([sys.executable, str(EXPERIMENTS / task / "make_repo.py"),
                               variant, str(dest)], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, proc.stderr
        repos[(task, variant)] = (dest, proc.stdout.strip())
    return repos


@pytest.mark.parametrize(("task", "variant"), EXPERIMENT_GRAPHS)
def test_experiment_graphs_flatten_validly(experiment_repos, task, variant):
    graphs = EXPERIMENTS / task / variant / "graphs"
    original = load_graph(graphs / "handwritten.json")
    flat = flatten_graph(original)
    assert_flattened(original, flat)
    repo, commit = experiment_repos[(task, variant)]
    assert flat.base_commit == commit
    report = validate(flat, build_index(repo, commit))
    assert report.ok, report.format()
    assert {issue.code for issue in report.warnings} <= {"W2"}
    committed = load_graph(graphs / "single_planned.json")
    assert committed == flat, (
        f"{graphs / 'single_planned.json'} is stale; regenerate it with "
        f"python -m aqours_code.taskgraph flatten {graphs / 'handwritten.json'} --out ...")
