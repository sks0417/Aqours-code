"""Checks for the job platform experiment task in experiments/taskgraph/job_platform/."""
from __future__ import annotations

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

import pytest

from aqours_code.taskgraph import build_index, derive_edges, load_graph, validate
from aqours_code.taskgraph.validate import ancestors

TASK = Path(__file__).resolve().parents[2] / "experiments" / "taskgraph" / "job_platform"
VARIANTS = ("coupled", "modular")
GROUPS = ("priority", "recurring", "dependencies", "rate_limit", "notifications",
          "audit_stats", "api", "dashboard", "end_to_end")
REGRESSION_TESTS = 33
MODULAR_FEATURES = ("P", "R", "D", "L", "N", "H")


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


def group_of(test_id: str) -> str:
    """``regression`` or the hidden test group (file name without ``test_``)."""
    module = test_id.split("::")[0]
    if ".regression." in module:
        return "regression"
    return module.rsplit(".", 1)[-1].removeprefix("test_")


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    """Build each variant twice (base, base + reference) and run its tests once.

    The four repositories are built and tested in parallel.
    """
    root = tmp_path_factory.mktemp("job_platform")

    def build(variant: str, with_reference: bool) -> dict:
        repo = root / f"{variant}_{'ref' if with_reference else 'base'}"
        commit = make_repo(variant, repo, with_reference)
        shutil.copytree(TASK / "hidden_tests", repo / "_hidden_tests")
        return {"path": repo, "commit": commit}

    keys = [(variant, with_reference) for variant in VARIANTS for with_reference in (False, True)]
    with ThreadPoolExecutor(max_workers=2 * len(keys)) as pool:
        built = dict(zip(keys, pool.map(lambda key: build(*key), keys)))
        runs = {(key, target): pool.submit(run_pytest, repo["path"], target,
                                           root / f"{repo['path'].name}_{target}.xml")
                for key, repo in built.items() for target in ("tests", "_hidden_tests")}
        for (key, target), future in runs.items():
            built[key]["public" if target == "tests" else "hidden"] = future.result()
    return lambda variant, with_reference: built[(variant, with_reference)]


def by_group(outcomes: dict[str, str]) -> dict[str, Counter]:
    groups: dict[str, Counter] = {}
    for test_id, outcome in outcomes.items():
        groups.setdefault(group_of(test_id), Counter())[outcome] += 1
    return groups


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
def test_base_passes_regression_and_fails_new_groups(repos, variant):
    built = repos(variant, False)
    code, public = built["public"]
    assert code == 0 and public and set(public.values()) == {"passed"}
    groups = by_group(built["hidden"][1])
    assert groups["regression"] == Counter(passed=REGRESSION_TESTS)
    assert set(groups) == {"regression", *GROUPS}
    for group in GROUPS:
        counts = groups[group]
        assert 8 <= counts.total() <= 15, (group, counts)
        assert counts["passed"] <= 1, (group, counts)
    new_tests = sum(groups[group].total() for group in GROUPS)
    assert 90 <= new_tests <= 120


@pytest.mark.parametrize("variant", VARIANTS)
def test_reference_passes_public_and_hidden_tests(repos, variant):
    built = repos(variant, True)
    for code, outcomes in (built["public"], built["hidden"]):
        assert code == 0 and outcomes
        assert set(outcomes.values()) == {"passed"}, outcomes
    groups = by_group(built["hidden"][1])
    assert groups["regression"]["passed"] == REGRESSION_TESTS
    assert len(built["public"][1]) > len(repos(variant, False)["public"][1])


@pytest.mark.parametrize("variant", VARIANTS)
def test_graphs_validate_and_handwritten_needs_no_derived_edges(repos, variant):
    index = build_index(repos(variant, False)["path"], repos(variant, False)["commit"])
    for name in ("single", "handwritten"):
        graph = load_graph(TASK / variant / "graphs" / f"{name}.json")
        report = validate(graph, index)
        assert report.ok, report.format()
        # Without G, nothing in modular requires the feature modules' symbols;
        # final_checks and the hidden tests use them (W2, unused provides).
        allowed = {"W2"} if (variant, name) == ("modular", "handwritten") else set()
        assert {issue.code for issue in report.warnings} <= allowed, report.format()
    handwritten = load_graph(TASK / variant / "graphs" / "handwritten.json")
    derived, entries = derive_edges(handwritten, index)
    assert entries == []
    assert derived.edges == handwritten.edges


@pytest.mark.parametrize("variant", VARIANTS)
def test_contract_writes_no_test_file(variant):
    graph = load_graph(TASK / variant / "graphs" / "handwritten.json")
    contract = next(node for node in graph.nodes if node.id == "A")
    assert contract.edit_set.create == [path for path in contract.edit_set.create
                                        if not path.startswith("tests/")]
    assert contract.check.commands == ["python -m pytest -q tests"]
    assert "test_contract" not in contract.goal


def critical_path_nodes(graph) -> int:
    successors = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        successors[edge.from_].append(edge.to)

    @cache
    def longest(node_id: str) -> int:
        return 1 + max((longest(nxt) for nxt in successors[node_id]), default=0)

    return max(longest(node.id) for node in graph.nodes)


def test_modular_graph_structure():
    graph = load_graph(TASK / "modular" / "graphs" / "handwritten.json")
    ancestor = ancestors(graph)
    for first in MODULAR_FEATURES:
        for second in MODULAR_FEATURES:
            if first != second:
                assert first not in ancestor[second], (first, second)
        assert ancestor[first] == {"A"}
    kinds = {node.id: node.kind for node in graph.nodes}
    assert kinds == {"A": "contract", **{node: "implement" for node in MODULAR_FEATURES}}
    edited = [set(node.edit_set.modify) | set(node.edit_set.create)
              for node in graph.nodes if node.id in MODULAR_FEATURES]
    for files in edited:
        assert not files & {"jobrunner/runner.py", "jobrunner/api.py", "jobrunner/dashboard.py"}
    assert critical_path_nodes(graph) == 2


def test_coupled_graph_structure():
    graph = load_graph(TASK / "coupled" / "graphs" / "handwritten.json")
    ancestor = ancestors(graph)
    chain = ("A", "S", "R", "N", "H")
    for earlier, later in zip(chain, chain[1:]):
        assert earlier in ancestor[later]
    for parallel in ("E", "F"):
        assert ancestor[parallel] == {"A"}
    assert {"H", "E", "F"} <= ancestor["G"]
    assert "tests/test_platform.py" in next(node for node in graph.nodes
                                           if node.id == "G").edit_set.create
    modular = load_graph(TASK / "modular" / "graphs" / "handwritten.json")
    assert critical_path_nodes(graph) > critical_path_nodes(modular)
    assert critical_path_nodes(graph) == 6
    for name in VARIANTS:
        single = load_graph(TASK / name / "graphs" / "single.json")
        assert len(single.nodes) == 1 and single.nodes[0].kind == "implement"
