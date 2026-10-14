"""Planner v3, Step 3: the graph built from hand-written task facts (no model)."""
from __future__ import annotations

import json

import pytest

import aqours_code.taskgraph.synthesize as synthesize_module
from aqours_code.taskgraph.affected import TestImports, scan_sources
from aqours_code.taskgraph.facts import Facts
from aqours_code.taskgraph.planner import PLANNER_VERSION, GraphInfo
from aqours_code.taskgraph.repo_index import RepoIndex
from aqours_code.taskgraph.schema import graph_to_json
from aqours_code.taskgraph.synthesize import (AFFECTED_TESTS_MAX, SMALL_SHARED_EDIT_LINES,
                                              SMALL_UNIT_LINES, WAIT_LINES, FactsConflict,
                                              SynthesisError, parse_ablate, synthesize)
from aqours_code.taskgraph.validate import validate

FINAL = "python -m pytest -q tests"
@pytest.fixture
def keep_split(monkeypatch):
    """Keep a chain of nodes split, for tests of other rules than parallelism (P1)."""
    monkeypatch.setattr(synthesize_module, "PARALLEL_PATH_MAX", 1.0)


INFO = GraphInfo(request_id="r", request="the request", repo="repo", base_commit="abc",
                 final_checks=[FINAL], model="m")


# ── builders ──

def logic(path: str, *symbols: str, lines: int = 80, standalone: bool = False) -> dict:
    return {"file": path, "kind": "logic", "symbols": [f"{path}::{s}" for s in symbols],
            "lines": lines, "standalone": standalone}


def registration(path: str, *names: str, lines: int = 1) -> dict:
    return {"file": path, "kind": "registration", "symbols": [f"{path}::{n}" for n in names],
            "lines": lines, "note": f"export {', '.join(names)}"}


def use(symbol: str, need: str = "interface") -> dict:
    return {"symbol": symbol, "need": need}


def unit(uid: str, *, size: int = 120, edits=(), create=(), provides=(), uses=(), tests=(),
         context=(), item: str | None = None) -> dict:
    return {"id": uid, "item": item or uid, "title": f"title {uid}", "goal": f"goal {uid}",
            "size": size, "edits": list(edits), "create": list(create),
            "provides": list(provides), "uses": list(uses), "tests": list(tests),
            "context_files": list(context)}


def facts(*units: dict, together=(), conventions=()) -> Facts:
    return Facts.model_validate({"conventions": list(conventions), "units": list(units),
                                 "together": list(together)})


def index(*files: str) -> RepoIndex:
    return RepoIndex(commit="abc", files=set(files), symbols=set())


def run(the_facts: Facts, the_index: RepoIndex, tests: TestImports | None = None, **kw):
    result = synthesize(the_facts, the_index, INFO, tests, **kw)
    report = validate(result.graph, the_index)
    assert report.ok, report.format()
    return result


def nodes(result) -> dict:
    return {node.id: node for node in result.graph.nodes}


def edges(result, kind: str | None = None) -> set[tuple[str, str]]:
    return {(edge.from_, edge.to) for edge in result.graph.edges
            if kind is None or edge.type == kind}


def rules(result, prop: str | None = None) -> list[str]:
    return [f"{d.property}/{d.rule}" for d in result.decisions
            if prop is None or d.property == prop]


def structure(result) -> dict:
    """Nodes, edges and decisions, as reported."""
    return {"nodes": {node.id: {"kind": node.kind, "modify": node.edit_set.modify,
                                "create": node.edit_set.create, "requires": node.requires,
                                "requires_impl": node.requires_impl,
                                "check": node.check.commands,
                                "post_merge": node.check.post_merge_commands}
                      for node in result.graph.nodes},
            "edges": sorted(f"{e.from_} -> {e.to} ({e.type})" for e in result.graph.edges),
            "decisions": [d.to_dict() for d in result.decisions]}


# ── the click shape: eight independent tickets, five of them exporting a name ──

INIT = "src/click/__init__.py"
MODULES = ["types", "core", "termui", "utils", "formatting", "decorators", "parser",
           "shell_completion"]
CLICK_FILES = (INIT, *(f"src/click/{name}.py" for name in MODULES), "tests/test_basic.py",
               "tests/test_imports.py", "README.md")


def click_units(own_use: str = "") -> list[dict]:
    units = []
    for number, name in enumerate(MODULES):
        uid = "DEFGHIJK"[number]
        path = f"src/click/{name}.py"
        edits = [logic(path, f"Feature{uid}", lines=100)]
        provides = [f"{path}::Feature{uid}"]
        uses = []
        if number < 5:
            edits.append(registration(INIT, f"Feature{uid}"))
            provides.append(f"{INIT}::Feature{uid}")
            if uid in own_use:
                uses.append(use(f"{INIT}::Feature{uid}"))
        test = f"tests/test_{name}_feature.py"
        units.append(unit(uid, size=150, edits=edits, create=[test], provides=provides,
                          uses=uses, tests=[test]))
    return units


CLICK_TESTS = scan_sources({"tests/test_basic.py": "import click\n",
                            "tests/test_imports.py": "from click.types import ParamType\n"},
                           set(CLICK_FILES))


def test_1_click_without_users_of_the_names_runs_eight_nodes_in_parallel_then_reg():
    result = run(facts(*click_units()), index(*CLICK_FILES), CLICK_TESTS)
    graph_nodes = nodes(result)
    assert list(graph_nodes) == [*"DEFGHIJK", "REG"]
    assert result.pre is None and result.reg == "REG"
    assert edges(result) == {(uid, "REG") for uid in "DEFGH"}
    reg = graph_nodes["REG"]
    assert reg.run_after_failed_upstream and reg.edit_set.modify == [INIT]
    assert reg.provides == [f"{INIT}::Feature{uid}" for uid in "DEFGH"]
    assert reg.check.post_merge_commands == [FINAL]
    for uid in "DEFGH":
        node = graph_nodes[uid]
        assert INIT not in node.edit_set.modify
        assert (f"Another step registers Feature{uid} in {INIT}; do not edit that file, "
                "and import from click.") in node.goal
    for uid in "IJK":
        assert "Another step" not in graph_nodes[uid].goal
    assert rules(result, "P2") == ["P2/registration_last"] * 5
    assert not any(node.run_after_failed_upstream for node in result.graph.nodes[:-1])
    # D's module is imported by an existing test: its own test plus that one
    assert graph_nodes["D"].check.commands == [
        "python -m pytest -q tests/test_types_feature.py tests/test_imports.py"]
    assert graph_nodes["E"].check.commands == ["python -m pytest -q tests/test_core_feature.py"]
    assert graph_nodes["D"].check.post_merge_commands is None


def test_2_click_where_a_unit_imports_its_own_registered_name():
    result = run(facts(*click_units(own_use="D")), index(*CLICK_FILES), CLICK_TESTS)
    graph_nodes = nodes(result)
    assert INIT in graph_nodes["D"].edit_set.modify
    assert f"{INIT}::FeatureD" in graph_nodes["D"].provides
    assert "Another step" not in graph_nodes["D"].goal
    assert graph_nodes["REG"].provides == [f"{INIT}::Feature{uid}" for uid in "EFGH"]
    assert edges(result) >= {("D", "REG"), *((uid, "REG") for uid in "EFGH")}
    assert rules(result, "P2") == ["P2/registration_kept", *["P2/registration_last"] * 4]
    # D and REG both edit __init__.py: both run the whole suite after merging
    assert graph_nodes["D"].check.post_merge_commands == [FINAL]
    assert any(d.rule == "ordered" and INIT in d.reason for d in result.decisions)


def test_3_a_name_another_unit_uses_is_registered_first_with_its_interface():
    the_facts = facts(
        unit("A", edits=[logic("pkg/dur.py", "Duration"), registration("pkg/__init__.py",
                                                                          "Duration")],
             provides=["pkg/dur.py::Duration", "pkg/__init__.py::Duration"]),
        unit("B", edits=[logic("pkg/opts.py", "Option")], provides=["pkg/opts.py::Option"],
             uses=[use("pkg/__init__.py::Duration")]))
    result = run(the_facts, index("pkg/__init__.py", "pkg/dur.py", "pkg/opts.py"))
    graph_nodes = nodes(result)
    assert list(graph_nodes) == ["PRE", "A", "B"]
    pre = graph_nodes["PRE"]
    assert pre.kind == "contract"
    assert set(pre.edit_set.modify) == {"pkg/__init__.py", "pkg/dur.py"}
    assert set(pre.provides) == {"pkg/__init__.py::Duration", "pkg/dur.py::Duration"}
    assert "The interface of `pkg/dur.py::Duration`" in pre.goal
    assert "register `pkg/__init__.py::Duration`" in pre.goal
    assert edges(result, "interface") == {("PRE", "A"), ("PRE", "B")}
    assert ("A", "B") not in edges(result)
    assert graph_nodes["A"].edit_set.modify == ["pkg/dur.py"]
    assert graph_nodes["B"].requires == ["pkg/__init__.py::Duration"]
    assert rules(result, "P2") == ["P2/registration_first"]


JP_FILES = ("jobs/models.py", "jobs/store.py", "jobs/api.py", "tests/test_api.py")


def jp_facts() -> Facts:
    priority = "jobs/models.py::Job.priority"
    return facts(
        unit("M", size=100, edits=[logic("jobs/models.py", "Job", lines=40)],
             create=["tests/test_priority.py"], provides=[priority],
             tests=["tests/test_priority.py"]),
        unit("X", size=150, create=["jobs/fair.py", "tests/test_fair.py"],
             provides=["jobs/fair.py::fair_order"], uses=[use(priority)],
             tests=["tests/test_fair.py"]),
        unit("Y", size=150, create=["jobs/quota.py", "tests/test_quota.py"],
             provides=["jobs/quota.py::check_quota"], uses=[use(priority)],
             tests=["tests/test_quota.py"]),
        unit("Z", size=120, edits=[logic("jobs/store.py", "JobStore.next_job")],
             create=["tests/test_next.py"], provides=["jobs/store.py::JobStore.next_job"],
             uses=[use(priority)], tests=["tests/test_next.py"]),
        unit("API", size=150, edits=[logic("jobs/api.py", "routes")],
             create=["tests/test_api_routes.py"], provides=["jobs/api.py::routes"],
             uses=[use("jobs/fair.py::fair_order"), use("jobs/quota.py::check_quota")],
             tests=["tests/test_api_routes.py"]))


def test_4_jp_interfaces_go_first_and_everything_else_runs_in_parallel():
    result = run(jp_facts(), index(*JP_FILES))
    graph_nodes = nodes(result)
    assert list(graph_nodes) == ["PRE", "M", "X", "Y", "Z", "API"]
    pre = graph_nodes["PRE"]
    assert pre.provides == ["jobs/models.py::Job.priority", "jobs/fair.py::fair_order",
                            "jobs/quota.py::check_quota"]
    assert pre.edit_set.modify == ["jobs/models.py"]
    assert pre.edit_set.create == ["jobs/fair.py", "jobs/quota.py"]
    assert not any(path.startswith("tests/") for path in pre.edit_set.create)
    # X and Y fill in the modules PRE creates
    assert graph_nodes["X"].edit_set.modify == ["jobs/fair.py"]
    assert graph_nodes["X"].edit_set.create == ["tests/test_fair.py"]
    assert edges(result) == {("PRE", key) for key in ("M", "X", "Y", "Z", "API")}
    assert graph_nodes["API"].requires == ["jobs/fair.py::fair_order",
                                           "jobs/quota.py::check_quota"]
    assert rules(result, "P2") == ["P2/interface_first"] * 5
    assert all(d.numbers["threshold"] == WAIT_LINES for d in result.decisions
               if d.property == "P2")


def test_5_behavior_and_small_interfaces_wait_without_pre():
    result = run(facts(
        unit("A", size=100, edits=[logic("a.py", "f")], provides=["a.py::f"]),
        unit("B", size=100, edits=[logic("b.py", "g")], provides=["b.py::g"],
             uses=[use("a.py::f", "behavior")]),
        unit("C", size=WAIT_LINES - 20, edits=[logic("c.py", "h")], provides=["c.py::h"]),
        unit("D", size=100, edits=[logic("d.py", "k")], provides=["d.py::k"],
             uses=[use("c.py::h", "interface")])),
        index("a.py", "b.py", "c.py", "d.py"))
    assert result.pre is None and result.reg is None
    assert edges(result) == {("A", "B"), ("C", "D")}
    assert edges(result, "full") == {("A", "B"), ("C", "D")}
    assert rules(result, "P2") == ["P2/wait_for_behavior", "P2/wait_small"]
    graph_nodes = nodes(result)
    assert graph_nodes["B"].requires_impl == ["a.py::f"]
    assert graph_nodes["D"].requires == ["c.py::h"]


def test_6_units_changing_the_same_function_are_merged():
    result = run(facts(
        unit("A", edits=[logic("m.py", "f")], provides=["m.py::f"]),
        unit("B", edits=[logic("m.py", "f", "g")], create=["tests/test_b.py"],
             tests=["tests/test_b.py"]),
        unit("C", edits=[logic("c.py", "h")])), index("m.py", "c.py"))
    assert list(nodes(result)) == ["A_B", "C"]
    assert result.node_units == {"A_B": ["A", "B"], "C": ["C"]}
    merged = nodes(result)["A_B"]
    assert "This sub-task combines 2 parts" in merged.goal
    assert rules(result, "P3") == ["P3/same_symbol_merged"]


def shared_facts(standalone: bool) -> Facts:
    return facts(*(unit(uid, size=100,
                        edits=[logic("config.py", f"SETTING_{uid}", lines=10,
                                     standalone=standalone)],
                        create=[f"feature_{uid.lower()}.py", f"tests/test_{uid.lower()}.py"],
                        provides=[f"config.py::SETTING_{uid}"],
                        tests=[f"tests/test_{uid.lower()}.py"])
                   for uid in "ABC"))


def test_7_small_standalone_shared_edits_go_first_others_are_ordered(keep_split):
    first = run(shared_facts(standalone=True), index("config.py"))
    graph_nodes = nodes(first)
    assert graph_nodes["PRE"].edit_set.modify == ["config.py"]
    assert graph_nodes["PRE"].provides == [f"config.py::SETTING_{uid}" for uid in "ABC"]
    assert all("config.py" not in graph_nodes[uid].edit_set.modify for uid in "ABC")
    assert edges(first) == {("PRE", uid) for uid in "ABC"}
    assert rules(first, "P3") == ["P3/small_shared_edit_first"] * 3
    assert all(d.numbers == {"lines": 10, "threshold": SMALL_SHARED_EDIT_LINES}
               for d in first.decisions if d.rule == "small_shared_edit_first")

    kept = run(shared_facts(standalone=False), index("config.py"))
    assert kept.pre is None
    assert edges(kept, "order") == {("A", "B"), ("A", "C"), ("B", "C")}
    ordered = [d for d in kept.decisions if d.rule == "ordered"]
    assert len(ordered) == 1 and "cannot be written on its own" in ordered[0].reason
    assert all(node.check.post_merge_commands == [FINAL] for node in kept.graph.nodes)


def test_8_mutual_behavior_is_merged_and_an_interface_cycle_is_opened_by_pre(keep_split):
    merged = run(facts(
        unit("A", edits=[logic("a.py", "x")], provides=["a.py::x"],
             uses=[use("b.py::y", "behavior")]),
        unit("B", edits=[logic("b.py", "y")], provides=["b.py::y"],
             uses=[use("a.py::x", "behavior")]),
        unit("C", edits=[logic("c.py", "z")])), index("a.py", "b.py", "c.py"))
    assert list(nodes(merged)) == ["A_B", "C"]
    assert rules(merged, "P1") == ["P1/mutual_behavior_merged", "P1/parallel_enough"]

    for size in (100, WAIT_LINES - 10):
        opened = run(facts(
            unit("A", size=size, edits=[logic("a.py", "x")], provides=["a.py::x"],
                 uses=[use("b.py::y", "interface")]),
            unit("B", edits=[logic("b.py", "y")], provides=["b.py::y"],
                 uses=[use("a.py::x", "behavior")])), index("a.py", "b.py"))
        assert list(nodes(opened)) == ["PRE", "A", "B"]
        assert nodes(opened)["PRE"].provides == ["b.py::y"]
        assert edges(opened) == {("PRE", "A"), ("PRE", "B"), ("A", "B")}
        assert rules(opened, "P2") == ["P2/interface_first", "P2/wait_for_behavior"]
    assert "on a cycle of uses" in [d for d in opened.decisions
                                    if d.rule == "interface_first"][0].reason


def cyclic_facts() -> Facts:
    return facts(
        unit("A", edits=[logic("a.py", "x")], create=["notes/a.md"], provides=["a.py::x"],
             uses=[use("b.py::y", "behavior")]),
        unit("B", edits=[logic("b.py", "y")], provides=["b.py::y"], context=["notes/a.md"]))


def test_9_a_cycle_left_after_step_2_is_a_contradiction_for_the_model():
    with pytest.raises(FactsConflict) as excinfo:
        synthesize(cyclic_facts(), index("a.py", "b.py"), INFO)
    issue = excinfo.value.issues[0]
    assert issue.code == "CONFLICT" and issue.nodes == ["A", "B"]
    assert "A uses b.py::y (behavior)" in issue.message
    assert "B reads notes/a.md, which A creates" in issue.message
    assert not isinstance(excinfo.value, SynthesisError)


def test_10_a_broken_move_is_a_program_error(monkeypatch):
    def lose(source, target, placed):
        source.edits.remove(placed)  # never arrives in the target

    monkeypatch.setattr(synthesize_module, "_move_edit", lose)
    with pytest.raises(SynthesisError) as excinfo:
        synthesize(facts(*click_units()), index(*CLICK_FILES), INFO, CLICK_TESTS)
    error = excinfo.value
    assert error.step.startswith("step 2")
    assert any(f"the edit of D on {INIT} is in no node" in problem
               for problem in error.problems)
    assert error.recent and "registration_last" in error.recent[-1]
    assert not isinstance(error, FactsConflict)


# ── step 4: checks ──

CHECK_FILES = {"src/click/types.py", "src/click/__init__.py", "mypkg/__init__.py",
               "mypkg/util.py", "tests/helpers.py", "tests/test_types.py",
               "tests/test_pkg.py", "tests/test_helpers.py", "tests/conftest.py",
               "tests/test_other.py"}
CHECK_SOURCES = {
    "tests/test_types.py": "import pytest\nfrom click.types import ParamType\n",
    "tests/test_pkg.py": "def test_x():\n    from mypkg import util\n",
    "tests/test_helpers.py": "from .helpers import helper\n",
    "tests/test_other.py": "import os\n",
}


def test_11_checks_run_own_and_affected_tests_from_src_and_top_level_packages():
    tests = scan_sources(CHECK_SOURCES, CHECK_FILES)
    result = run(facts(
        unit("A", edits=[logic("src/click/types.py", "Duration")],
             create=["tests/test_duration.py"], tests=["tests/test_duration.py"]),
        unit("B", edits=[logic("mypkg/util.py", "helper2")]),
        unit("C", edits=[logic("tests/helpers.py", "helper")]),
        unit("D", create=["mypkg/new.py"])), index(*CHECK_FILES), tests)
    graph_nodes = nodes(result)
    assert graph_nodes["A"].check.commands == [
        "python -m pytest -q tests/test_duration.py tests/test_types.py"]
    assert graph_nodes["B"].check.commands == ["python -m pytest -q tests/test_pkg.py"]
    assert graph_nodes["C"].check.commands == ["python -m pytest -q tests/test_helpers.py"]
    assert graph_nodes["D"].check.commands == ["python -m compileall -q mypkg/new.py"]
    assert all(node.check.post_merge_commands is None for node in result.graph.nodes)
    assert rules(result, "P4") == ["P4/affected_tests"] * 3 + ["P4/compile_only"]


def test_11_too_many_affected_tests_or_unreadable_imports_run_the_whole_suite():
    many = {f"tests/test_{n}.py": "import core\n" for n in range(AFFECTED_TESTS_MAX + 1)}
    files = {"core.py", "other.py", *many}
    result = run(facts(unit("A", edits=[logic("core.py", "f")]),
                       unit("B", edits=[logic("other.py", "g")])),
                 index(*files), scan_sources(many, files))
    assert nodes(result)["A"].check.commands == [FINAL]
    fallback = [d for d in result.decisions if d.rule == "full_suite_fallback"]
    assert fallback[0].numbers == {"affected": AFFECTED_TESTS_MAX + 1,
                                   "threshold": AFFECTED_TESTS_MAX}
    assert nodes(result)["B"].check.commands == ["python -m compileall -q other.py"]

    broken = scan_sources({"tests/test_bad.py": "def broken(:\n"}, {"tests/test_bad.py",
                                                                    "core.py", "other.py"})
    assert broken.unresolved == ["tests/test_bad.py"]
    result = run(facts(unit("A", edits=[logic("core.py", "f")]),
                       unit("B", edits=[logic("other.py", "g")])),
                 index("core.py", "other.py", "tests/test_bad.py"), broken)
    assert all(node.check.commands == [FINAL] for node in result.graph.nodes)


def test_11_conftest_changes_affect_the_tests_below_it():
    tests = scan_sources(CHECK_SOURCES, CHECK_FILES)
    assert tests.affected(["tests/conftest.py"]) == sorted(CHECK_SOURCES)
    assert tests.affected(["tests/test_other.py"]) == ["tests/test_other.py"]


def test_11_nodes_editing_a_shared_file_check_the_whole_suite_after_merging():
    result = run(facts(unit("A", edits=[logic("m.py", "f")], create=["tests/test_a.py"],
                            tests=["tests/test_a.py"]),
                       unit("B", edits=[logic("m.py", "g")]),
                       unit("C", edits=[logic("c.py", "h")])), index("m.py", "c.py"))
    graph_nodes = nodes(result)
    assert graph_nodes["A"].check.post_merge_commands == [FINAL]
    assert graph_nodes["B"].check.post_merge_commands == [FINAL]
    assert graph_nodes["C"].check.post_merge_commands is None
    assert graph_nodes["A"].check.commands == ["python -m pytest -q tests/test_a.py"]


# ── P1: together, small units, a single node ──

def test_together_and_small_units_are_merged_and_one_group_is_the_single_graph(keep_split):
    the_facts = facts(
        unit("A", edits=[logic("a.py", "f")], provides=["a.py::f"]),
        unit("B", edits=[logic("b.py", "g")]),
        unit("S", size=SMALL_UNIT_LINES - 10, edits=[logic("b.py", "h")]),
        unit("T", size=10, edits=[logic("t.py", "k")]),
        together=[{"units": ["A", "B"], "reason": "one feature"}])
    result = run(the_facts, index("a.py", "b.py", "t.py"))
    assert result.node_units == {"A_B_S": ["A", "B", "S"], "T": ["T"]}
    assert rules(result, "P1") == ["P1/together", "P1/small_merged", "P1/small_kept",
                                   "P1/parallel_enough"]
    small = [d for d in result.decisions if d.rule == "small_merged"][0]
    assert small.numbers == {"size": 20, "threshold": SMALL_UNIT_LINES, "relation": 1}

    single = synthesize(facts(unit("A", edits=[logic("a.py", "f")]),
                              unit("B", edits=[logic("b.py", "g")]),
                              together=[{"units": ["A", "B"], "reason": "one"}]),
                        index("a.py", "b.py"), INFO)
    assert [node.id for node in single.graph.nodes] == ["solve"]
    assert single.graph.nodes[0].edit_set.any_file
    assert single.graph.nodes[0].check.commands == [FINAL]
    assert rules(single) == ["P1/together", "P1/single_unit"]
    assert single.node_units == {"solve": ["A", "B"]}


def test_a_small_unit_joins_the_most_related_group_first_in_order():
    result = run(facts(
        unit("A", edits=[logic("a.py", "f")], provides=["a.py::f"]),
        unit("B", edits=[logic("b.py", "g"), logic("c.py", "h")]),
        unit("S", size=10, edits=[logic("b.py", "k"), logic("c.py", "m")],
             uses=[use("a.py::f")])), index("a.py", "b.py", "c.py"))
    # S shares two files with B and one use with A
    assert {key: units for key, units in result.node_units.items() if key != "PRE"} == {
        "A": ["A"], "B_S": ["B", "S"]}
    assert [d.numbers["relation"] for d in result.decisions if d.rule == "small_merged"] == [2]


# ── registration_too_large, ablation, determinism ──

def test_a_large_registration_is_treated_as_logic():
    result = run(facts(
        unit("A", edits=[logic("a.py", "f"), registration("pkg.py", "f", lines=8)],
             provides=["pkg.py::f"]),
        unit("B", edits=[logic("b.py", "g"), registration("pkg.py", "g")],
             provides=["pkg.py::g"])), index("a.py", "b.py", "pkg.py"))
    assert rules(result, "P3")[0] == "P3/registration_too_large"
    decision = result.decisions[0]
    assert decision.numbers == {"lines": 8, "threshold": 5} and decision.units == ["A"]
    graph_nodes = nodes(result)
    assert "pkg.py" in graph_nodes["A"].edit_set.modify  # logic stays with A
    assert graph_nodes["REG"].provides == ["pkg.py::g"]
    assert ("A", "REG") in edges(result, "order")


def test_ablate_p1_ignores_together_and_small_units_but_merges_mutual_behavior():
    the_facts = facts(
        unit("A", edits=[logic("a.py", "f")], provides=["a.py::f"],
             uses=[use("b.py::g", "behavior")]),
        unit("B", edits=[logic("b.py", "g")], provides=["b.py::g"],
             uses=[use("a.py::f", "behavior")]),
        unit("C", edits=[logic("c.py", "h")]),
        unit("D", edits=[logic("d.py", "k")]),
        unit("S", size=5, edits=[logic("d.py", "m")]),
        together=[{"units": ["C", "D"], "reason": "one"}])
    result = run(the_facts, index("a.py", "b.py", "c.py", "d.py"), ablate={"p1"})
    assert result.node_units == {"A_B": ["A", "B"], "C": ["C"], "D": ["D"], "S": ["S"]}
    assert rules(result, "P1") == ["P1/mutual_behavior_merged", "P1/ablated", "P1/ablated",
                                   "P1/ablated"]  # together, small unit, parallelism
    assert result.graph.generator.ablate == ["p1"]


def test_ablate_p2_waits_for_every_use_and_keeps_registrations():
    result = run(jp_facts(), index(*JP_FILES), ablate={"p2"})
    assert result.pre is None
    assert edges(result, "full") == {("M", "X"), ("M", "Y"), ("M", "Z"), ("X", "API"),
                                     ("Y", "API")}
    assert set(rules(result, "P2")) == {"P2/ablated"}
    click = run(facts(*click_units()), index(*CLICK_FILES), ablate={"p2"})
    assert click.reg is None
    assert all(INIT in nodes(click)[uid].edit_set.modify for uid in "DEFGH")
    assert edges(click, "order") >= {("D", "E"), ("E", "F")}


def test_ablate_p3_keeps_same_function_and_small_shared_edits_in_place(keep_split):
    result = run(shared_facts(standalone=True), index("config.py"), ablate={"p3"})
    assert result.pre is None
    assert edges(result, "order") == {("A", "B"), ("A", "C"), ("B", "C")}
    assert set(rules(result, "P3")) == {"P3/ablated"}
    same = run(facts(unit("A", edits=[logic("m.py", "f")]),
                     unit("B", edits=[logic("m.py", "f")])), index("m.py"), ablate={"p3"})
    assert list(nodes(same)) == ["A", "B"] and edges(same, "order") == {("A", "B")}


def test_ablate_p4_runs_the_whole_suite_everywhere():
    result = run(jp_facts(), index(*JP_FILES), ablate={"p4"})
    for node in result.graph.nodes:
        assert node.check.commands == [FINAL]
        assert node.check.post_merge_commands == [FINAL]
    assert set(rules(result, "P4")) == {"P4/ablated"}
    assert result.graph.generator.ablate == ["p4"]


def test_ablate_names_are_checked():
    assert parse_ablate("p1, P3") == frozenset({"p1", "p3"})
    assert parse_ablate("") == frozenset()
    with pytest.raises(ValueError, match="p5"):
        parse_ablate("p1,p5")
    with pytest.raises(ValueError):
        synthesize(jp_facts(), index(*JP_FILES), INFO, ablate={"p9"})


def test_decisions_and_graph_are_deterministic():
    first = synthesize(jp_facts(), index(*JP_FILES), INFO)
    second = synthesize(jp_facts(), index(*JP_FILES), INFO)
    assert [d.to_dict() for d in first.decisions] == [d.to_dict() for d in second.decisions]
    assert graph_to_json(first.graph) == graph_to_json(second.graph)
    assert first.graph.generator.planner_version == PLANNER_VERSION == "planner-v3.1"
    assert first.graph.generator.ablate is None
    # P3: PRE and M order on jobs/models.py, PRE creates the modules X and Y fill in;
    # P4: one check per node, and a whole-suite post-merge check for PRE, M, X, Y
    assert first.counts() == {"P1": 1, "P2": 5, "P3": 3, "P4": 10}
    assert all(d.nodes for d in first.decisions)


def test_conventions_end_every_goal_and_pre_writes_no_tests():
    result = run(facts(*jp_facts().model_dump()["units"], conventions=["Use the clock."]),
                 index(*JP_FILES))
    assert all(node.goal.rstrip().endswith("- Use the clock.") for node in result.graph.nodes)
    pre = nodes(result)["PRE"]
    assert "no tests" in pre.goal and pre.check.post_merge_commands == [FINAL]


def print_structures():  # pragma: no cover - used to write the report of task 25
    for name, result in (("test 1", run(facts(*click_units()), index(*CLICK_FILES),
                                        CLICK_TESTS)),
                         ("test 2", run(facts(*click_units(own_use="D")),
                                        index(*CLICK_FILES), CLICK_TESTS)),
                         ("test 4", run(jp_facts(), index(*JP_FILES)))):
        print(f"## {name}\n{json.dumps(structure(result), indent=1)}")


# ── task 25b: registrations several nodes keep in one file ──

def test_registrations_several_nodes_need_early_go_first_instead_of_queuing_the_nodes():
    result = run(facts(*click_units(own_use="DEFGH")), index(*CLICK_FILES), CLICK_TESTS)
    graph_nodes = nodes(result)
    pre = graph_nodes["PRE"]
    names = [f"{INIT}::Feature{uid}" for uid in "DEFGH"]
    targets = [f"src/click/{MODULES[n]}.py::Feature{uid}" for n, uid in enumerate("DEFGH")]
    assert set(pre.provides) == {*names, *targets}
    items = [line for line in pre.goal.splitlines() if line[:1].isdigit()]
    assert [line.split(". ", 1)[1].split(" ", 2)[:2] for line in items] == (
        [["The", "interface"]] * 5 + [["In", "src/click/__init__.py,"]] * 5)
    assert pre.edit_set.modify[0] == INIT
    assert result.reg is None
    # no edge between D-K; D-H start after PRE, all at the same time
    assert edges(result) == {("PRE", uid) for uid in "DEFGH"}
    for uid in "DEFGH":
        assert INIT not in graph_nodes[uid].edit_set.modify
        assert graph_nodes[uid].requires == [f"{INIT}::Feature{uid}"]
    shared = [d for d in result.decisions if d.rule == "registration_first_shared"]
    assert [d.units for d in shared] == [[uid] for uid in "DEFGH"]
    assert all(d.property == "P3" and d.numbers == {"nodes_sharing": 5} for d in shared)
    assert "the whole nodes would queue on src/click/__init__.py" in shared[0].reason


def test_a_registration_whose_symbol_is_unknown_is_kept_and_ordered(keep_split):
    def registrar(uid: str, name: str) -> dict:
        path = f"pkg/{uid.lower()}.py"
        return unit(uid, edits=[logic(path, f"real_{uid}"), registration("pkg/__init__.py",
                                                                          name)],
                    provides=[f"{path}::real_{uid}", f"pkg/__init__.py::{name}"],
                    uses=[use(f"pkg/__init__.py::{name}")])

    files = ("pkg/__init__.py", "pkg/a.py", "pkg/b.py", "pkg/c.py")
    both = run(facts(registrar("A", "alias_a"), registrar("B", "alias_b")), index(*files))
    assert both.pre is None
    assert edges(both, "order") == {("A", "B")}
    assert rules(both, "P3")[:2] == ["P3/registration_kept_ordered"] * 2
    kept = [d for d in both.decisions if d.rule == "registration_kept_ordered"][0]
    assert "is not known" in kept.reason and "queues, as a whole node" in kept.reason

    mixed = run(facts(registrar("A", "alias_a"), registrar("B", "real_B"),
                      registrar("C", "real_C")), index(*files))
    assert [d.rule for d in mixed.decisions if d.property == "P3"][:3] == [
        "registration_first_shared", "registration_first_shared",
        "registration_kept_ordered"]
    assert ("PRE", "A") in edges(mixed, "order")  # A queues behind PRE on __init__.py
    assert {("PRE", "B"), ("PRE", "C")} <= edges(mixed, "interface")
    assert "pkg/__init__.py" in nodes(mixed)["A"].edit_set.modify


def test_a_small_unit_sharing_only_a_registration_file_is_not_merged():
    def small(path: str, kind: str) -> dict:
        edit = (registration("pkg/__init__.py", "small") if kind == "registration"
                else logic("pkg/__init__.py", "small"))
        return unit("S", size=10, edits=[logic(path, "s"), edit])

    big = unit("B", edits=[logic("pkg/b.py", "b"), registration("pkg/__init__.py", "b")])
    other = unit("O", edits=[logic("pkg/o.py", "o")])
    files = ("pkg/__init__.py", "pkg/b.py", "pkg/s.py", "pkg/o.py")
    alone = run(facts(big, small("pkg/s.py", "registration"), other), index(*files))
    assert {"B", "S", "O"} <= set(alone.node_units)
    assert rules(alone, "P1") == ["P1/small_kept", "P1/parallel_enough"]
    merged = run(facts(big, small("pkg/s.py", "logic"), other), index(*files))
    assert {key: units for key, units in merged.node_units.items() if key != "REG"} == {
        "B_S": ["B", "S"], "O": ["O"]}
    assert rules(merged, "P1") == ["P1/small_merged", "P1/parallel_enough"]


def test_affected_tests_follow_imports_through_the_source_modules():
    files = {"pkg/__init__.py", "pkg/a.py", "pkg/b.py", "pkg/c.py", "tests/test_pkg.py",
             "tests/test_c.py"}
    tests = scan_sources({"pkg/__init__.py": "from . import a\n",
                          "pkg/a.py": "from pkg import b\n",
                          "pkg/b.py": "import pkg.a\n",  # a cycle: a -> b -> a
                          "pkg/c.py": "",
                          "tests/test_pkg.py": "import pkg\n",
                          "tests/test_c.py": "from pkg import c\n"}, files)
    # test_c imports pkg.c, which runs pkg/__init__.py, which imports a (and a imports b)
    assert tests.affected(["pkg/a.py"]) == ["tests/test_c.py", "tests/test_pkg.py"]
    assert tests.affected(["pkg/b.py"]) == ["tests/test_c.py", "tests/test_pkg.py"]
    assert tests.affected(["pkg/c.py"]) == ["tests/test_c.py"]
    assert tests.reached("tests/test_pkg.py") == {"pkg/__init__.py", "pkg/a.py", "pkg/b.py"}
    # every import of a package runs its __init__.py, so test_c reaches a and b too
    assert tests.reached("tests/test_c.py") == {"pkg/__init__.py", "pkg/a.py", "pkg/b.py",
                                                "pkg/c.py"}
    result = run(facts(unit("A", edits=[logic("pkg/a.py", "f")]),
                       unit("C", edits=[logic("pkg/c.py", "g")])), index(*files), tests)
    assert nodes(result)["A"].check.commands == [
        "python -m pytest -q tests/test_c.py tests/test_pkg.py"]


def test_a_source_file_that_does_not_parse_runs_the_whole_suite():
    files = {"pkg/__init__.py", "pkg/a.py", "pkg/broken.py", "tests/test_pkg.py"}
    tests = scan_sources({"pkg/__init__.py": "", "pkg/a.py": "", "pkg/broken.py": "def (:\n",
                          "tests/test_pkg.py": "import pkg.a\n"}, files)
    assert tests.unresolved == ["pkg/broken.py"]
    result = run(facts(unit("A", edits=[logic("pkg/a.py", "f")]),
                       unit("B", edits=[logic("pkg/__init__.py", "g")])), index(*files), tests)
    assert all(node.check.commands == [FINAL] for node in result.graph.nodes)
    assert "pkg/broken.py" in [d for d in result.decisions
                               if d.rule == "full_suite_fallback"][0].reason


def test_a_source_module_does_not_resolve_names_from_its_own_directory():
    files = {"src/click/__init__.py", "src/click/core.py", "src/click/types.py",
             "tests/helpers.py", "tests/test_x.py"}
    tests = scan_sources({"src/click/core.py": "import types\n",
                          "tests/test_x.py": "import helpers\n"}, files)
    assert tests.sources["src/click/core.py"] == set()  # the standard library's types
    assert tests.imports["tests/test_x.py"] == {"tests/helpers.py"}


# ── task 25c, section 2: P1 gives a graph that is not parallel enough to one agent ──

def chain_facts() -> Facts:
    return facts(
        unit("A", size=100, edits=[logic("a.py", "f")], provides=["a.py::f"]),
        unit("B", size=100, edits=[logic("b.py", "g")], provides=["b.py::g"],
             uses=[use("a.py::f", "behavior")]),
        unit("C", size=100, edits=[logic("c.py", "h")], uses=[use("b.py::g", "behavior")]))


def test_a_chain_of_nodes_goes_to_one_agent():
    result = run(chain_facts(), index("a.py", "b.py", "c.py"))
    assert [node.id for node in result.graph.nodes] == ["solve"]
    assert result.graph.nodes[0].edit_set.any_file
    assert result.node_units == {"solve": ["A", "B", "C"]}
    par = result.parallelism
    assert par == {"node_work": {"A": 100, "B": 100, "C": 100}, "total": 300,
                   "critical": 300, "ratio": 1.0, "path": ["A", "B", "C"],
                   "threshold": 0.7, "collapsed": True}
    last = result.decisions[-1]
    assert (last.property, last.rule, last.nodes) == ("P1", "low_parallelism_single", ["solve"])
    assert last.numbers == {"critical": 300, "total": 300, "ratio": 1.0, "threshold": 0.7}
    assert "A -> B -> C has 300 lines, 100% of all 300 lines, more than 70%" in last.reason
    # the decisions before it describe the split graph that was not used
    assert [d.rule for d in result.decisions[:2]] == ["wait_for_behavior"] * 2


def test_independent_nodes_stay_split():
    result = run(facts(*(unit(uid, size=100, edits=[logic(f"{uid.lower()}.py", "f")])
                         for uid in "ABC")), index("a.py", "b.py", "c.py"))
    assert [node.id for node in result.graph.nodes] == ["A", "B", "C"]
    assert result.parallelism["ratio"] == pytest.approx(0.3333, abs=1e-4)
    assert not result.parallelism["collapsed"]
    assert result.decisions[-1].rule == "parallel_enough"
    assert result.decisions[-1].nodes == ["A"]  # the heaviest chain: one node, first on a tie


def test_work_moved_into_pre_counts_for_pre_not_for_its_unit():
    result = run(shared_facts(standalone=True), index("config.py"))
    work = result.parallelism["node_work"]
    assert work == {"PRE": 30, "A": 90, "B": 90, "C": 90}
    assert result.parallelism["critical"] == 120 and result.parallelism["path"] == ["PRE", "A"]
    jp = run(jp_facts(), index(*JP_FILES)).parallelism
    # PRE: three interfaces of INTERFACE_LINES each, and no moved edit
    assert jp["node_work"]["PRE"] == 3 * synthesize_module.INTERFACE_LINES


def test_ablate_p1_keeps_a_chain_split():
    result = run(chain_facts(), index("a.py", "b.py", "c.py"), ablate={"p1"})
    assert [node.id for node in result.graph.nodes] == ["A", "B", "C"]
    assert result.parallelism["ratio"] == 1.0 and not result.parallelism["collapsed"]
    assert result.decisions[-1].rule == "ablated" and result.decisions[-1].property == "P1"


def test_order_edges_count_for_the_critical_path():
    # no symbols: the two edits of m.py are ordered, so the nodes form a chain
    result = run(facts(unit("A", edits=[logic("m.py")]), unit("B", edits=[logic("m.py")]),
                       unit("C", size=60, edits=[logic("c.py", "h")])), index("m.py", "c.py"))
    assert result.parallelism["path"] == ["A", "B"]
    assert result.parallelism["critical"] == 240 and result.parallelism["total"] == 300
    assert result.parallelism["collapsed"] and result.graph.nodes[0].id == "solve"


def test_one_group_has_no_parallelism_figures():
    single = synthesize(facts(unit("A", edits=[logic("a.py", "f")])), index("a.py"), INFO)
    assert single.parallelism is None


# ── task 25c, section 3: a behavior use of a class that only holds data ──

def class_facts(provider_edit: dict, used: str, why: str = "") -> Facts:
    return facts(
        unit("M", edits=[provider_edit], provides=[f"models.py::{used}"]),
        unit("U", edits=[logic("ui.py", "page")],
             uses=[{"symbol": f"models.py::{used}", "need": "behavior", "why": why}]))


def test_a_behavior_use_of_a_data_class_is_an_interface_use():
    result = run(class_facts(logic("models.py", "RateLimit"), "RateLimit"),
                 index("models.py", "ui.py"))
    decision = [d for d in result.decisions if d.rule == "class_use_interface"]
    assert len(decision) == 1 and decision[0].property == "P2" and decision[0].units == ["U"]
    assert decision[0].reason == ("U uses RateLimit, a class whose methods no unit changes, "
                                  "so only its constructor and fields are needed: interface")
    assert "P2/interface_first" in rules(result, "P2")  # no longer a wait
    assert nodes(result)["U"].requires == ["models.py::RateLimit"]


def test_a_class_whose_methods_a_unit_changes_keeps_the_behavior_use():
    result = run(class_facts(logic("models.py", "RateLimit", "RateLimit.allowed"),
                             "RateLimit", why="the limits allowed computes"),
                 index("models.py", "ui.py"))
    assert "class_use_interface" not in [d.rule for d in result.decisions]
    assert rules(result, "P2") == ["P2/wait_for_behavior"]


@pytest.mark.parametrize("name", ["SCHEMA", "JOB_FIELDS", "_Private", "render"])
def test_names_that_are_not_classes_keep_the_behavior_use(name):
    result = synthesize(class_facts(logic("models.py", name), name),
                        index("models.py", "ui.py"), INFO)
    assert "class_use_interface" not in [d.rule for d in result.decisions]


def test_ablate_p2_skips_the_class_check():
    result = synthesize(class_facts(logic("models.py", "RateLimit"), "RateLimit"),
                        index("models.py", "ui.py"), INFO, ablate={"p2"})
    assert "class_use_interface" not in [d.rule for d in result.decisions]
