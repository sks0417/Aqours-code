"""Checks for the click batch experiment task in experiments/taskgraph/click_batch/."""
from __future__ import annotations

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aqours_code.taskgraph import build_index, derive_edges, load_graph, validate
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.compare import critical_path_length
from aqours_code.taskgraph.validate import ancestors

TASK = Path(__file__).resolve().parents[2] / "experiments" / "taskgraph" / "click_batch"
TICKETS = ("t1_duration", "t2_select", "t3_result", "t4_nushell", "t5_exclusive",
           "t6_size", "t7_bullets", "t8_hint")
# ticket node -> the one module it edits; the first four replace a stub of contract A
TICKET_MODULES = {
    "T1": "src/click/types.py", "T2": "src/click/termui.py",
    "T5": "src/click/decorators.py", "T6": "src/click/utils.py",
    "T3": "src/click/testing.py", "T4": "src/click/shell_completion.py",
    "T7": "src/click/formatting.py", "T8": "src/click/exceptions.py",
}
AFTER_CONTRACT = ("T1", "T2", "T5", "T6")
INIT = "src/click/__init__.py"


def make_repo(dest: Path, with_reference: bool = False) -> str:
    args = [sys.executable, str(TASK / "make_repo.py"), str(dest)]
    if with_reference:
        args.append("--with-reference")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def run_pytest(repo: Path, target: str, xml: Path) -> tuple[int, dict[str, str]]:
    """Run pytest in ``repo``; return the exit code and outcome per test id."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", target, "-p", "no:cacheprovider",
         f"--junitxml={xml}"],
        cwd=repo, capture_output=True, text=True, timeout=600)
    outcomes = {}
    for case in ET.parse(xml).getroot().iter("testcase"):
        test_id = f"{case.get('classname')}::{case.get('name')}"
        if case.find("failure") is not None or case.find("error") is not None:
            outcomes[test_id] = "failed"
        elif case.find("skipped") is not None:  # skip and xfail
            outcomes[test_id] = "skipped"
        else:
            outcomes[test_id] = "passed"
    return proc.returncode, outcomes


def group_of(test_id: str) -> str:
    """``regression`` or the ticket (file name without ``test_``)."""
    module = test_id.split("::")[0]
    if ".regression." in f"{module}.":
        return "regression"
    return module.split(".")[1].removeprefix("test_")


def by_group(outcomes: dict[str, str]) -> dict[str, Counter]:
    groups: dict[str, Counter] = {}
    for test_id, outcome in outcomes.items():
        groups.setdefault(group_of(test_id), Counter())[outcome] += 1
    return groups


def lf_files(root: Path) -> dict[str, bytes]:
    """Relative path -> content with LF line endings, without bytecode."""
    return {path.relative_to(root).as_posix(): path.read_bytes().replace(b"\r\n", b"\n")
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts}


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    """Build base and base + reference, and run the public and hidden tests of each once."""
    root = tmp_path_factory.mktemp("click_batch")

    def build(with_reference: bool) -> dict:
        # The single graph records the directory name of the base repository.
        repo = root / ("reference" if with_reference else "click_batch")
        commit = make_repo(repo, with_reference)
        shutil.copytree(TASK / "hidden_tests", repo / "_hidden_tests",
                        ignore=shutil.ignore_patterns("__pycache__"))
        return {"path": repo, "commit": commit}

    with ThreadPoolExecutor(max_workers=4) as pool:
        built = dict(zip((False, True), pool.map(build, (False, True))))
        runs = {(key, target): pool.submit(run_pytest, repo["path"], target,
                                           root / f"{repo['path'].name}_{target}.xml")
                for key, repo in built.items() for target in ("tests", "_hidden_tests")}
        for (key, target), future in runs.items():
            built[key]["public" if target == "tests" else "hidden"] = future.result()
    return built


def test_make_repo_is_deterministic_and_graphs_use_its_commit(repos):
    commit = repos[False]["commit"]
    assert len(commit) == 40
    assert repos[True]["commit"] == commit  # a second run of make_repo.py
    request = (TASK / "request.md").read_text(encoding="utf-8").strip()
    for name in ("single", "handwritten"):
        graph = load_graph(TASK / "graphs" / f"{name}.json")
        assert graph.base_commit == commit
        assert graph.request.strip() == request
        assert graph.final_checks == ["python -m pytest -q tests"]


def test_snapshot_content_and_regression_copy():
    vendor = TASK / "vendor" / "click"
    assert {path.name for path in vendor.iterdir()} == {
        "src", "tests", "pyproject.toml", "README.md", "LICENSE.txt"}
    assert {path.name for path in (vendor / "src").iterdir()} == {"click"}
    assert "BSD-3-Clause" in (vendor / "pyproject.toml").read_text(encoding="utf-8")
    assert "Copyright 2014 Pallets" in (vendor / "LICENSE.txt").read_text(encoding="utf-8")
    # regression/ is click's own tests, unchanged
    assert lf_files(TASK / "hidden_tests" / "regression") == lf_files(vendor / "tests")
    hidden = sorted(path.stem.removeprefix("test_")
                    for path in (TASK / "hidden_tests").glob("test_*.py"))
    assert hidden == sorted(TICKETS)
    assert sorted(path.stem for path in (TASK / "reference").iterdir()) == sorted(TICKETS)


def test_base_passes_its_own_tests_and_fails_every_ticket_test(repos):
    base = repos[False]
    code, public = base["public"]
    assert code == 0 and "failed" not in public.values()
    assert Counter(public.values())["passed"] > 2000
    groups = by_group(base["hidden"][1])
    assert set(groups) == {"regression", *TICKETS}
    assert groups["regression"] == Counter(public.values())
    for ticket in TICKETS:
        counts = groups[ticket]
        assert 6 <= counts.total() <= 10, (ticket, counts)
        assert counts == Counter(failed=counts.total()), (ticket, counts)


def test_reference_passes_public_and_hidden_tests(repos):
    base, reference = repos[False], repos[True]
    for code, outcomes in (reference["public"], reference["hidden"]):
        assert code == 0 and outcomes
        assert "failed" not in outcomes.values(), outcomes
    # the reference adds a public test file per ticket
    assert len(reference["public"][1]) > len(base["public"][1])
    groups = by_group(reference["hidden"][1])
    assert groups["regression"] == by_group(base["hidden"][1])["regression"]
    for ticket in TICKETS:
        assert groups[ticket] == Counter(passed=groups[ticket].total()), ticket


def test_graphs_validate_on_the_base_repository(repos):
    index = build_index(repos[False]["path"], repos[False]["commit"])
    for name in ("single", "handwritten"):
        graph = load_graph(TASK / "graphs" / f"{name}.json")
        report = validate(graph, index)
        assert report.ok, report.format()
        # Nothing in the graph consumes what the tickets add (W2, unused
        # provides): final_checks and the hidden tests do.
        allowed = {"W2"} if name == "handwritten" else set()
        assert {issue.code for issue in report.warnings} <= allowed, report.format()
    handwritten = load_graph(TASK / "graphs" / "handwritten.json")
    derived, entries = derive_edges(handwritten, index)
    assert entries == []
    assert derived.edges == handwritten.edges


def test_single_graph_is_what_the_single_command_writes(repos, tmp_path):
    output = tmp_path / "single.json"
    assert main(["single", str(TASK / "request.md"), "--repo", str(repos[False]["path"]),
                 "--out", str(output), "--final-check", "python -m pytest -q tests"]) == 0
    committed = (TASK / "graphs" / "single.json").read_bytes().replace(b"\r\n", b"\n")
    assert output.read_bytes() == committed


def test_graph_structure():
    single = load_graph(TASK / "graphs" / "single.json")
    assert len(single.nodes) == 1 and single.nodes[0].kind == "implement"
    assert single.nodes[0].edit_set.any_file

    graph = load_graph(TASK / "graphs" / "handwritten.json")
    nodes = {node.id: node for node in graph.nodes}
    assert {node_id: node.kind for node_id, node in nodes.items()} == {
        "A": "contract", **{node_id: "implement" for node_id in TICKET_MODULES}}
    # The contract exports the four public names and writes no test file.
    contract = nodes["A"]
    assert INIT in contract.edit_set.modify and contract.edit_set.create == []
    assert sorted(contract.edit_set.modify) == sorted(
        [INIT, *(TICKET_MODULES[node_id] for node_id in AFTER_CONTRACT)])
    assert contract.check.commands == ["python -m pytest -q tests"]
    # Each ticket edits its own module and creates its own test file.
    for node_id, module in TICKET_MODULES.items():
        node = nodes[node_id]
        assert node.edit_set.modify == [module]
        assert len(node.edit_set.create) == 1
        assert node.edit_set.create[0].startswith("tests/test_")
        assert node.check.commands == ["python -m pytest -q tests",
                                       f"python -m pytest -q {node.edit_set.create[0]}"]
    created = [nodes[node_id].edit_set.create[0] for node_id in TICKET_MODULES]
    assert len(set(created)) == len(created)
    # The tickets are parallel: only the four that replace a stub wait for A.
    ancestor = ancestors(graph)
    for node_id in TICKET_MODULES:
        assert ancestor[node_id] == ({"A"} if node_id in AFTER_CONTRACT else set())
    assert {(edge.from_, edge.to, edge.type) for edge in graph.edges} == {
        ("A", node_id, "interface") for node_id in AFTER_CONTRACT}
    assert critical_path_length(graph) == 2
