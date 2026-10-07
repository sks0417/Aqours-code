"""Checks for the job runner experiment task in experiments/taskgraph/job_runner/."""
from __future__ import annotations

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from functools import cache
from pathlib import Path

import pytest

from aqours_code.taskgraph import build_index, derive_edges, load_graph, validate
from aqours_code.taskgraph.validate import ancestors

TASK = Path(__file__).resolve().parents[2] / "experiments" / "taskgraph" / "job_runner"
VARIANTS = ("coupled", "modular")
FEATURE_FILES = ("test_cancellation.py", "test_retry.py", "test_recovery.py")


def make_repo(variant: str, dest: Path, with_reference: bool = False) -> str:
    args = [sys.executable, str(TASK / "make_repo.py"), variant, str(dest)]
    if with_reference:
        args.append("--with-reference")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def run_pytest(repo: Path, target: str, xml: Path) -> tuple[int, dict[str, str]]:
    """Run pytest in ``repo``; return the exit code and outcome per test id."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", target, "-p", "no:cacheprovider",
         f"--junitxml={xml}"],
        cwd=repo, capture_output=True, text=True, timeout=120)
    outcomes = {}
    for case in ET.parse(xml).getroot().iter("testcase"):
        test_id = f"{case.get('classname')}::{case.get('name')}"
        failed = case.find("failure") is not None or case.find("error") is not None
        outcomes[test_id] = "failed" if failed else "passed"
    return proc.returncode, outcomes


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    """Build each variant twice (base, base + reference) and run its tests once."""
    root = tmp_path_factory.mktemp("job_runner")

    @cache
    def build(variant: str, with_reference: bool) -> dict:
        repo = root / f"{variant}_{'ref' if with_reference else 'base'}"
        commit = make_repo(variant, repo, with_reference)
        shutil.copytree(TASK / "hidden_tests", repo / "_hidden_tests")
        public = run_pytest(repo, "tests", root / f"{repo.name}_public.xml")
        hidden = run_pytest(repo, "_hidden_tests", root / f"{repo.name}_hidden.xml")
        return {"path": repo, "commit": commit, "public": public, "hidden": hidden}

    return build


@pytest.mark.parametrize("variant", VARIANTS)
def test_graph_base_commits_match_generated_repo(repos, variant):
    commit = repos(variant, False)["commit"]
    assert repos(variant, True)["commit"] == commit
    request = (TASK / "request.md").read_text(encoding="utf-8").strip()
    for name in ("single", "handwritten"):
        graph = load_graph(TASK / variant / "graphs" / f"{name}.json")
        assert graph.base_commit == commit
        assert graph.request.strip() == request


@pytest.mark.parametrize("variant", VARIANTS)
def test_base_passes_public_tests_and_fails_feature_tests(repos, variant):
    built = repos(variant, False)
    code, public = built["public"]
    assert code == 0 and public and set(public.values()) == {"passed"}
    _, hidden = built["hidden"]
    modules = tuple(f"{Path(name).stem}::" for name in FEATURE_FILES)
    feature = {test: outcome for test, outcome in hidden.items()
               if any(module in test for module in modules)}
    assert len(feature) >= 3 * 4
    assert set(feature.values()) == {"failed"}, feature


@pytest.mark.parametrize("variant", VARIANTS)
def test_reference_passes_public_and_hidden_tests(repos, variant):
    built = repos(variant, True)
    for code, outcomes in (built["public"], built["hidden"]):
        assert code == 0 and outcomes
        assert set(outcomes.values()) == {"passed"}, outcomes
    assert len(built["hidden"][1]) >= 6 * 4


@pytest.mark.parametrize("variant", VARIANTS)
def test_graphs_validate_and_handwritten_needs_no_derived_edges(repos, variant):
    index = build_index(repos(variant, False)["path"], repos(variant, False)["commit"])
    for name in ("single", "handwritten"):
        graph = load_graph(TASK / variant / "graphs" / f"{name}.json")
        report = validate(graph, index)
        assert report.ok, report.format()
        assert not report.warnings, report.format()
    handwritten = load_graph(TASK / variant / "graphs" / "handwritten.json")
    derived, entries = derive_edges(handwritten, index)
    assert entries == []
    assert derived.edges == handwritten.edges


def critical_path_nodes(graph) -> int:
    successors = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        successors[edge.from_].append(edge.to)

    @cache
    def longest(node_id: str) -> int:
        return 1 + max((longest(nxt) for nxt in successors[node_id]), default=0)

    return max(longest(node.id) for node in graph.nodes)


def test_coupled_graph_structure():
    graph = load_graph(TASK / "coupled" / "graphs" / "handwritten.json")
    ancestor = ancestors(graph)
    parallel = ("B", "C", "E", "F")
    for first in parallel:
        for second in parallel:
            if first != second:
                assert first not in ancestor[second], (first, second)
    assert "C" in ancestor["D"]
    assert {"B", "D", "E", "F"} <= ancestor["G"]
    single = load_graph(TASK / "coupled" / "graphs" / "single.json")
    assert len(single.nodes) == 1 and single.nodes[0].kind == "implement"


def test_modular_graph_structure():
    graph = load_graph(TASK / "modular" / "graphs" / "handwritten.json")
    files = {path for node in graph.nodes
             for path in (*node.edit_set.modify, *node.edit_set.create)}
    persistence_only = [node.id for node in graph.nodes
                        if set(node.edit_set.modify) == {"jobrunner/store.py"}]
    assert not persistence_only
    assert "jobrunner/scheduler.py" in files and "jobrunner/recovery.py" in files
    coupled = load_graph(TASK / "coupled" / "graphs" / "handwritten.json")
    assert critical_path_nodes(graph) < critical_path_nodes(coupled)
    assert (critical_path_nodes(graph), critical_path_nodes(coupled)) == (3, 4)
