"""Planner v3 with scripted model answers: facts, checks, prompt, run_plan and the CLI."""
from __future__ import annotations

import json
import re

import pytest

import aqours_code.taskgraph.synthesize as synthesize_module
from aqours_code.taskgraph import load_graph
from aqours_code.taskgraph.affected import scan_sources
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.facts import Facts, check_facts, parse_facts
from aqours_code.taskgraph.planner import (DraftError, DraftItem, GraphInfo, PlanOptions,
                                           extract_json_block, report_path_for, run_plan)
from aqours_code.taskgraph.planner_v3 import (FACTS_PROMPT_PATH, FactsRevision,
                                              build_facts_prompt, load_facts_prompt)
from aqours_code.taskgraph.repo_index import RepoIndex
from aqours_code.taskgraph.synthesize import synthesize
from aqours_code.taskgraph.validate import Issue, validate
from taskgraph_support import git, make_graph, make_node
from test_planner import (REQUEST, ScriptedDrafter, ScriptedPlanner, answer, items_answer,
                          patch_workers)

FINAL = "python -m pytest -q tests"


def unit(uid: str, **fields) -> dict:
    return {"id": uid, "item": uid, "title": f"title {uid}", "goal": f"goal {uid}",
            "size": 80, **fields}


def logic(path: str, *symbols: str, lines: int = 30, standalone: bool = False) -> dict:
    return {"file": path, "kind": "logic", "symbols": [f"{path}::{s}" for s in symbols],
            "lines": lines, "standalone": standalone}


# Two independent units on the toy repository.
TWO_UNITS = {"conventions": ["All storage access goes through JobStore."], "units": [
    unit("S", edits=[logic("store.py", "JobStore.cancel")], create=["tests/test_cancel.py"],
         provides=["store.py::JobStore.cancel"], tests=["tests/test_cancel.py"]),
    unit("R", edits=[logic("runner.py", "run_loop")], create=["tests/test_skip.py"],
         provides=["runner.py::run_loop"], tests=["tests/test_skip.py"],
         context_files=["README.md"]),
]}
UNKNOWN_USE = {"units": [{**TWO_UNITS["units"][0],
                          "uses": [{"symbol": "store.py::JobStore.nope", "need": "behavior"}]},
                         TWO_UNITS["units"][1]]}
# R needs S's real result, and S reads a file R creates: no interface can open that.
CONFLICTING = {"units": [
    unit("S", edits=[logic("store.py", "JobStore.cancel")], create=["notes.md"],
         provides=["store.py::JobStore.cancel"],
         uses=[{"symbol": "runner.py::run_loop", "need": "behavior"}]),
    unit("R", edits=[logic("runner.py", "run_loop")], provides=["runner.py::run_loop"],
         context_files=["notes.md"]),
]}


def plan_v3(toy_repo, tmp_path, answers: list[str], items=("S", "R"), **options):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    worker = ScriptedPlanner(answers)
    drafter = ScriptedDrafter([items_answer(list(items))])
    plan_options = PlanOptions(request_path=request, repo=toy_repo.path,
                               out=tmp_path / "out" / "graph.json",
                               final_checks=[FINAL], timeout_s=60, **options)
    result = run_plan(plan_options, worker, drafter)
    return result, worker, plan_options


# ── facts: format and checks ──

ITEMS = [DraftItem(id="S", title="item S"), DraftItem(id="R", title="item R")]


def toy_like_index() -> RepoIndex:
    return RepoIndex(commit="abc", files={"store.py", "runner.py", "README.md", "SPEC.md"},
                     markdown_titles={"SPEC.md": ["Cancel"]})


def codes(data: dict) -> list[tuple[str, list[str]]]:
    the_facts = Facts.model_validate(data)
    return [(issue.code, issue.nodes) for issue in check_facts(the_facts, ITEMS,
                                                                toy_like_index())]


def test_valid_facts_have_no_errors_and_ids_may_be_numbers():
    assert codes(TWO_UNITS) == []
    the_facts = parse_facts(answer({"units": [{**unit("S"), "id": "S", "item": 1,
                                               "edits": [logic("store.py", "f")]}],
                                    "together": [{"units": ["S"]}]}))
    assert the_facts.units[0].item == "1" and the_facts.units[0].edits[0].standalone is False


def test_facts_format_errors_name_the_unit():
    with pytest.raises(DraftError) as excinfo:
        parse_facts(answer({"units": [{**unit("S"), "edits": [{"file": "store.py",
                                                               "kind": "rewrite"}]}],
                            "nodes": []}))
    messages = [issue.format() for issue in excinfo.value.issues]
    assert all(message.startswith("[FORMAT]") for message in messages)
    assert any("units[0] (id 'S').edits[0].kind" in message for message in messages)
    assert any("units[0] (id 'S').edits[0].lines" in message for message in messages)
    assert any("nodes" in message for message in messages)


@pytest.mark.parametrize(("change", "expected"), [
    # F1: unknown item; an item without units
    (lambda d: d["units"][0].update(item="X"), [("F1", ["S"]), ("F1", [])]),
    # F2: repeated id
    (lambda d: d["units"][1].update(id="S", item="R"), [("F2", ["S"])]),
    # F3: a missing file, a file twice, a symbol of another file, nothing edited
    (lambda d: d["units"][0]["edits"].append(logic("nope.py", "f")), [("F3", ["S"])]),
    (lambda d: d["units"][0]["edits"].append(logic("store.py", "g")), [("F3", ["S"])]),
    (lambda d: d["units"][0]["edits"][0]["symbols"].append("runner.py::run_loop"),
     [("F3", ["S"])]),
    (lambda d: d["units"][1].update(edits=[], create=[], provides=[], tests=[],
                                    context_files=[]), [("F3", ["R"])]),
    # F4: an existing file created; two creators; edited and created
    (lambda d: d["units"][0]["create"].append("runner.py"), [("F4", ["S"])]),
    (lambda d: d["units"][1]["create"].append("tests/test_cancel.py"),
     [("F4", ["S", "R"])]),
    (lambda d: d["units"][0]["create"].append("store.py"), [("F4", ["S"]), ("F4", ["S"])]),
    # F5: a symbol outside the unit's files; a name its registration does not list
    (lambda d: d["units"][0]["provides"].append("runner.py::run_loop"), [("F5", ["S"])]),
    (lambda d: d["units"][0]["edits"].append(
        {"file": "README.py", "kind": "registration", "symbols": [], "lines": 1}),
     [("F3", ["S"])]),
    # F6: an unknown need; a symbol nobody provides
    (lambda d: d["units"][1].update(uses=[{"symbol": "store.py::JobStore.cancel",
                                           "need": "maybe"}]), [("F6", ["R"])]),
    (lambda d: d["units"][1].update(uses=[{"symbol": "store.py::JobStore.add",
                                           "need": "behavior"}]), [("F6", ["R"])]),
    # F7: a test file the unit neither creates nor edits
    (lambda d: d["units"][1]["tests"].append("tests/test_basic.py"), [("F7", ["R"])]),
    # F8: together names an unknown unit
    (lambda d: d.update(together=[{"units": ["S", "Q"], "reason": "x"}]), [("F8", [])]),
    # F9: a context file nobody has; a heading that does not exist
    (lambda d: d["units"][1]["context_files"].append("docs/none.md"), [("F9", ["R"])]),
    (lambda d: d["units"][1]["context_files"].append("SPEC.md#Missing"), [("F9", ["R"])]),
])
def test_every_facts_check(change, expected):
    data = json.loads(json.dumps(TWO_UNITS))
    change(data)
    assert codes(data) == expected


def new_file_facts() -> dict:
    """S creates config.py; R adds code to it."""
    data = json.loads(json.dumps(TWO_UNITS))
    data["units"][0]["create"].append("config.py")
    data["units"][0]["provides"].append("config.py::load")
    data["units"][1]["edits"].append(logic("config.py", "dump"))
    data["units"][1]["provides"].append("config.py::dump")
    return data


def messages(data: dict) -> list[tuple[str, list[str], str]]:
    the_facts = Facts.model_validate(data)
    return [(issue.code, issue.nodes, issue.message)
            for issue in check_facts(the_facts, ITEMS, toy_like_index())]


def test_a_unit_may_edit_a_new_file_another_unit_creates():
    assert codes(new_file_facts()) == []


def test_editing_a_new_file_nobody_creates_is_f3():
    data = new_file_facts()
    data["units"][0]["create"].remove("config.py")
    data["units"][0]["provides"].remove("config.py::load")
    assert messages(data) == [(
        "F3", ["R"],
        "edits config.py, which does not exist and no other unit creates it. A new file is "
        "listed in the `create` of exactly one unit; other units that add code to it list "
        "it in `edits`.")]


def test_a_new_file_created_by_two_units_is_f4():
    data = new_file_facts()
    data["units"][1]["edits"].pop()
    data["units"][1]["create"].append("config.py")
    assert messages(data) == [(
        "F4", ["S", "R"],
        "config.py is created by 2 units (S, R). One unit lists it in `create`; the other "
        "units that add code to it list it in `edits`, with the symbols they add.")]


def test_a_unit_that_creates_and_edits_one_new_file_is_told_to_only_create_it():
    data = new_file_facts()
    data["units"][0]["edits"].append(logic("config.py", "load"))
    assert messages(data) == [(
        "F4", ["S"],
        "lists config.py both in `create` and in `edits`; a file the unit creates is listed "
        "only in its `create`")]


def test_facts_prompt_says_how_units_share_a_new_file():
    text = _flat(load_facts_prompt())
    assert _flat("""- `create`: new files, tests included. They must not exist yet, and each new
        file is created by exactly one unit. When several units add code to the
        same new file, one unit lists it in `create` and the others list it in
        `edits`, with the symbols they add.""") in text
    assert "use what it provides" not in text


def test_a_registered_name_must_be_in_its_registration_edit():
    index = RepoIndex(commit="abc", files={"pkg/__init__.py", "pkg/a.py"})
    data = {"units": [unit("S", edits=[
        logic("pkg/a.py", "f"),
        {"file": "pkg/__init__.py", "kind": "registration", "symbols": ["pkg/__init__.py::f"],
         "lines": 1}], provides=["pkg/a.py::f", "pkg/__init__.py::g"])]}
    issues = check_facts(Facts.model_validate(data), [DraftItem(id="S", title="s")], index)
    assert [issue.code for issue in issues] == ["F5"]
    assert "a registered name must be in the `symbols`" in issues[0].message


# ── the prompt ──

def _flat(text: str) -> str:
    return " ".join(text.split())


def test_facts_prompt_leaves_the_structure_to_the_program():
    text = load_facts_prompt()
    assert _flat("""You describe the work; you do not design the task graph. The program
        decides which units become nodes, which shared edits are made first or
        last, what runs in parallel, and how each node is checked, from what you
        report. Report truthfully, even when two units edit the same file or the
        same function. When you are unsure whether an edit can be written on its
        own, say standalone false.""") in _flat(text)
    assert 'say "behavior"' not in text
    lowered = text.lower()
    for word in ("contract", "integration", "requires_impl", '"check"', "split rule"):
        assert word not in lowered, word
    for kept in ("offset", "limit", "outline", "conventions", "under 800 characters",
                 "SPEC.md#Heading"):
        assert kept in text, kept


def test_facts_prompt_mentions_nothing_of_the_experiment_tasks():
    text = FACTS_PROMPT_PATH.read_text(encoding="utf-8").lower()
    for word in ("job", "runner", "tenant", "webhook", "recurring", "rate limit",
                 "ratelimit", "audit", "notification", "priorit", "dashboard",
                 "cancel", "retry", "retries", "scheduler", "click", "duration"):
        assert word not in text, word


RECIPE_INDEX = RepoIndex(
    commit="abc",
    files={"recipes/__init__.py", "recipes/models.py", "recipes/book.py", "README.md",
           "SPEC.md"},
    markdown_titles={"SPEC.md": ["Star ratings", "Best-rated list", "Shopping-list export",
                                 "Command line"]})


def example_facts() -> Facts:
    text = load_facts_prompt()
    example = text[text.index("## Example"):]
    return parse_facts(example[:example.index("What the facts say")])


def test_the_example_shows_facts_only_and_every_kind_of_fact():
    text = load_facts_prompt()
    example = text[text.index("## Example"):]
    for word in ("contract", "node", "order", "parallel", "edge", "first", "wait"):
        assert not re.search(rf"\b{word}", example, re.IGNORECASE), word
    the_facts = example_facts()
    items = [DraftItem(id=str(n), title=f"item {n}") for n in range(1, 5)]
    assert check_facts(the_facts, items, RECIPE_INDEX) == []
    units = {unit.id: unit for unit in the_facts.units}
    registered = {name: unit.id for unit in the_facts.units
                  for name in unit.registration_names()}
    used = {use.symbol: use.need for unit in the_facts.units for use in unit.uses}
    others = [(unit.id, use.symbol) for unit in the_facts.units for use in unit.uses
              if registered.get(use.symbol) not in (None, unit.id)]
    assert others                                            # a name another unit uses
    assert any(name not in used for name in registered)      # a name nobody uses
    assert {"interface", "behavior"} <= set(used.values())
    # a field is used through its interface; every behavior use names its result
    assert used["recipes/models.py::Recipe.ratings"] == "interface"
    assert all(use.why for unit in the_facts.units for use in unit.uses
               if use.need == "behavior")
    assert the_facts.together == []
    assert any(use.symbol in units[uid].registration_names()  # a unit uses its own name
               for uid in units for use in units[uid].uses)


def test_the_example_facts_give_a_valid_graph():
    info = GraphInfo("r", "request", "repo", "abc", [FINAL], None)
    result = synthesize(example_facts(), RECIPE_INDEX, info)
    assert validate(result.graph, RECIPE_INDEX).ok
    # R and B are no longer one unit: B starts after PRE, which adds Recipe.ratings
    assert list(result.node_units) == ["PRE", "R", "B", "S", "C", "REG"]
    assert {(edge.from_, edge.to) for edge in result.graph.edges
            if edge.type == "full"} >= {("B", "C"), ("S", "REG")}
    assert not result.parallelism["collapsed"]


def test_facts_prompt_has_the_sections_and_the_revision(toy_index):
    items = [DraftItem(id="1", title="Cancel", description="stop a queued job")]
    prompt = build_facts_prompt(REQUEST, toy_index, items, "## models.py (1 lines)")
    assert prompt.startswith(load_facts_prompt().rstrip())
    order = [prompt.index(heading) for heading in ("# The request", "# Draft items",
                                                   "# Repository\n", "# Repository outline")]
    assert order == sorted(order)
    assert "- 1: **Cancel**. stop a queued job" in prompt
    assert "Every unit belongs to one of them" in prompt
    assert "Fix your previous answer" not in prompt
    revised = build_facts_prompt(REQUEST, toy_index, items, None, FactsRevision(
        1, 2, '{"units": []}', [Issue("F6", ["S"], "uses x, which no unit provides")]))
    assert "revision 1 of 2" in revised and '{"units": []}' in revised
    assert "[F6] S: uses x, which no unit provides" in revised


# ── run_plan v3 ──

def test_v3_is_the_default_and_builds_the_graph_from_the_facts(toy_repo, tmp_path):
    result, worker, options = plan_v3(toy_repo, tmp_path, [answer(TWO_UNITS)])
    assert result.success and len(worker.requests) == 1
    assert "You describe the work" in worker.requests[0].prompt
    graph = load_graph(options.out)
    assert graph.generator.planner_version == "planner-v3.1"
    assert graph.generator.revision_mode == "none" and graph.generator.ablate is None
    assert [node.id for node in graph.nodes] == ["S", "R"] and graph.edges == []
    by_id = {node.id: node for node in graph.nodes}
    # the toy repository's broken.py does not parse: which tests a change
    # affects is unknown, so every node runs the whole suite
    assert all(node.check.commands == [FINAL] for node in graph.nodes)
    assert by_id["S"].goal.endswith("- All storage access goes through JobStore.")
    assert by_id["R"].context_files == ["README.md"]
    report = json.loads(report_path_for(options.out).read_text(encoding="utf-8"))
    assert report["planner"] == "v3" and report["planner_version"] == "planner-v3.1"
    assert report["success"] and report["revision_rounds"] == 0
    assert report["revision_reasons"] == []
    assert report["facts"]["units"][0]["id"] == "S"
    assert report["node_units"] == {"S": ["S"], "R": ["R"]}
    assert report["decision_counts"] == {"P1": 1, "P2": 0, "P3": 0, "P4": 2}
    assert [d["rule"] for d in report["decisions"]] == [
        "full_suite_fallback", "full_suite_fallback", "parallel_enough"]
    assert "broken.py" in report["decisions"][0]["reason"]
    assert report["pre_node"] is None and report["reg_node"] is None
    assert report["ablate"] == [] and report["units"] == 2
    assert report["test_imports"] == {"files": 1, "sources": 3, "unresolved": ["broken.py"]}
    assert report["totals"]["model_calls"] == 2
    assert "unmerged_graph" not in report


def test_v3_checks_run_the_tests_that_reach_a_changed_file(toy_repo, tmp_path):
    git(toy_repo.path, "rm", "-q", "broken.py")
    git(toy_repo.path, "commit", "-q", "-m", "drop the broken file")
    result, _, options = plan_v3(toy_repo, tmp_path, [answer(TWO_UNITS)])
    by_id = {node.id: node for node in load_graph(options.out).nodes}
    # tests/test_basic.py imports store and runner: it is affected by both
    assert by_id["S"].check.commands == [
        "python -m pytest -q tests/test_cancel.py tests/test_basic.py"]
    assert by_id["R"].check.commands == [
        "python -m pytest -q tests/test_skip.py tests/test_basic.py"]
    assert result.report["test_imports"] == {"files": 1, "sources": 3, "unresolved": []}


def test_facts_errors_and_contradictions_are_revised_by_the_model(toy_repo, tmp_path):
    result, worker, options = plan_v3(
        toy_repo, tmp_path, ["no json here", answer(CONFLICTING), answer(TWO_UNITS)])
    assert result.success
    report = result.report
    assert report["revision_rounds"] == 2
    assert report["revision_reasons"] == [{"round": 1, "codes": ["FORMAT"]},
                                          {"round": 2, "codes": ["CONFLICT"]}]
    assert "[CONFLICT] S, R: the facts contradict each other" in worker.requests[2].prompt
    assert load_graph(options.out).generator.revision_mode == "llm"

    result, worker, _ = plan_v3(toy_repo, tmp_path, [answer(UNKNOWN_USE)] * 3)
    assert not result.success and len(worker.requests) == 3
    assert [[i["code"] for i in r["errors"]] for r in result.report["rounds"]] == [["F6"]] * 3
    assert not result.report["graph_written"] and result.report["decisions"] == []


def test_a_program_error_is_not_sent_back_to_the_model(toy_repo, tmp_path, monkeypatch):
    def lose(source, target, placed):
        source.edits.remove(placed)

    monkeypatch.setattr(synthesize_module, "_move_edit", lose)
    shared = {"units": [
        unit("S", edits=[logic("store.py", "JobStore.cancel"),
                         logic("models.py", "Job", lines=5, standalone=True)],
             create=["tests/test_cancel.py"], tests=["tests/test_cancel.py"]),
        unit("R", edits=[logic("runner.py", "run_loop"), logic("models.py", "JobStatus")]),
    ]}
    result, worker, options = plan_v3(toy_repo, tmp_path, [answer(shared)] * 3)
    assert not result.success and len(worker.requests) == 1
    errors = result.report["errors"]
    assert [issue["code"] for issue in errors] == ["SYNTH"]
    assert "the edit of S on models.py is in no node" in errors[0]["message"]
    assert not options.out.exists()


def test_one_group_left_gives_the_single_agent_graph(toy_repo, tmp_path):
    together = {**TWO_UNITS, "together": [{"units": ["S", "R"], "reason": "one feature"}]}
    result, _, options = plan_v3(toy_repo, tmp_path, [answer(together)])
    graph = load_graph(options.out)
    assert result.success and [node.id for node in graph.nodes] == ["solve"]
    assert graph.nodes[0].edit_set.any_file
    assert [d["rule"] for d in result.report["decisions"]] == ["together", "single_unit"]


def test_ablation_is_recorded_in_the_graph_and_the_report(toy_repo, tmp_path):
    result, _, options = plan_v3(toy_repo, tmp_path, [answer(TWO_UNITS)],
                                 ablate=frozenset({"p4", "p1"}))
    graph = load_graph(options.out)
    assert graph.generator.ablate == ["p1", "p4"]
    assert all(node.check.commands == [FINAL] for node in graph.nodes)
    assert result.report["ablate"] == ["p1", "p4"]


def test_the_fast_path_of_v3_is_marked_as_v3(toy_repo, tmp_path):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    options = PlanOptions(request_path=request, repo=toy_repo.path,
                          out=tmp_path / "graph.json", final_checks=[FINAL])
    result = run_plan(options, ScriptedPlanner([]),
                      ScriptedDrafter([items_answer(["1"], estimate=100)]))
    assert result.success and result.report["fast_path"]["taken"]
    assert load_graph(options.out).generator.planner_version == "planner-v3.1"


def test_an_unknown_planner_version_is_refused(toy_repo, tmp_path):
    options = PlanOptions(request_path=tmp_path / "r.md", repo=toy_repo.path,
                          out=tmp_path / "g.json", planner="v1")
    with pytest.raises(ValueError, match="unknown planner"):
        run_plan(options, ScriptedPlanner([]))


# ── the plan command ──

def run_cli(toy_repo, tmp_path, monkeypatch, answers, *flags):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = tmp_path / "graph.json"
    patch_workers(monkeypatch, ScriptedPlanner(answers), [items_answer(["S", "R"])])
    code = main(["plan", str(request), "--repo", str(toy_repo.path), "--out", str(out),
                 "--final-check", FINAL, *flags])
    return code, out


def test_plan_command_prints_the_v3_summary(toy_repo, tmp_path, monkeypatch, capsys):
    code, out = run_cli(toy_repo, tmp_path, monkeypatch, [answer(TWO_UNITS)])
    assert code == 0
    output = capsys.readouterr().out
    assert ("nodes: 2  edges: 0  PRE: no  REG: no  revision rounds: 0  success: yes"
            in output)
    assert "units: 2  decisions: P1: 1  P2: 0  P3: 0  P4: 2" in output
    assert "parallelism: critical path 80 / 160 lines (0.50 <= 0.70) -> split" in output
    assert "ablated" not in output and "nodes before merge" not in output
    assert f"wrote {out}" in output and f"wrote {report_path_for(out)}" in output


def test_plan_command_with_ablation(toy_repo, tmp_path, monkeypatch, capsys):
    code, out = run_cli(toy_repo, tmp_path, monkeypatch, [answer(TWO_UNITS)],
                        "--ablate", "p4")
    assert code == 0
    assert "ablated: p4 (rules turned off)" in capsys.readouterr().out
    assert load_graph(out).generator.ablate == ["p4"]


@pytest.mark.parametrize(("flags", "message"), [
    (["--ablate", "p5"], "--ablate: unknown properties: p5"),
    (["--planner", "v2", "--ablate", "p1"], "--ablate applies only to --planner v3"),
    (["--no-merge"], "--no-merge applies only to --planner v2"),
])
def test_plan_command_refuses_options_that_do_not_fit(toy_repo, tmp_path, monkeypatch, capsys,
                                                       flags, message):
    code, _ = run_cli(toy_repo, tmp_path, monkeypatch, [], *flags)
    assert code == 2 and message in capsys.readouterr().err


def test_plan_command_still_runs_v2(toy_repo, tmp_path, monkeypatch, capsys):
    from test_planner import VALID_DRAFT
    code, out = run_cli(toy_repo, tmp_path, monkeypatch, [answer(VALID_DRAFT)],
                        "--planner", "v2")
    assert code == 0
    assert load_graph(out).generator.planner_version == "planner-v2"
    assert "nodes before merge" in capsys.readouterr().out


# ── validation of the new check field ──

def test_blank_post_merge_commands_are_a_v4_error():
    node = make_node("A", modify=("a.py",))
    node["check"]["post_merge_commands"] = [" "]
    report = validate(make_graph([node]))
    assert [(issue.code, issue.message) for issue in report.errors] == [
        ("V4", "check.post_merge_commands has no non-empty command")]


def test_scan_reads_relative_imports_and_src_layout():
    files = {"src/pkg/__init__.py", "src/pkg/mod.py", "tests/sub/__init__.py",
             "tests/sub/helpers.py", "tests/sub/test_a.py", "top.py"}
    tests = scan_sources({"tests/sub/test_a.py": ("from . import helpers\n"
                                                  "import pkg.mod as m\nimport top\n")}, files)
    assert tests.imports["tests/sub/test_a.py"] == {
        "tests/sub/__init__.py", "tests/sub/helpers.py", "src/pkg/__init__.py",
        "src/pkg/mod.py", "top.py"}
    assert extract_json_block(answer({"a": 1})) == '{\n  "a": 1\n}\n'


def test_facts_prompt_asks_for_members_not_classes():
    text = _flat(load_facts_prompt())
    assert _flat("""For a change inside a class, name the member (`path.py::Class.method`,
        `path.py::Class.field`), not the class. Name the class itself only when the
        whole class is new or rewritten.""") in text
    edited = [symbol for unit in example_facts().units for edit in unit.edits
              if edit.kind == "logic" for symbol in edit.symbols]
    assert "recipes/models.py::Recipe.ratings" in edited
    assert "recipes/models.py::Recipe" not in edited


def test_facts_prompt_asks_for_a_reason_for_behavior():
    text = _flat(load_facts_prompt())
    for bullet in ('''- `need: "interface"`: the unit only needs the symbol to exist with its
        name and signature. This is the usual case: creating, reading or raising
        a class, a field or an exception, and calling a function or method whose
        result the unit's tests can replace with a small fake.''',
                   '''- `need: "behavior"`: the unit's code or tests depend on a result that only
        the provider's real code produces, so that a fake would make the tests
        meaningless. Say which result in `why`, in one sentence. If you cannot
        name such a result, the need is "interface".''',
                   '''- Name the method you call, not its class: `RecipeBook.rate`, not
        `RecipeBook`. A use of a class means its constructor and fields only.'''):
        assert _flat(bullet) in text
    assert "`why` is required when `need` is `\"behavior\"`" in text


def test_behavior_uses_without_a_reason_are_reported(toy_repo, tmp_path):
    data = json.loads(json.dumps(TWO_UNITS))
    data["units"][1]["uses"] = [
        {"symbol": "store.py::JobStore.cancel", "need": "behavior"},
        {"symbol": "store.py::JobStore.cancel", "need": "behavior", "why": "  "}]
    data["units"][0]["uses"] = [
        {"symbol": "runner.py::run_loop", "need": "behavior", "why": "the counts"}]
    data["units"][0]["edits"][0]["symbols"] = ["store.py::JobStore.cancel"]
    result, _, _ = plan_v3(toy_repo, tmp_path, [answer(data)])
    assert result.report["behavior_without_why"] == [
        {"unit": "R", "symbol": "store.py::JobStore.cancel"},
        {"unit": "R", "symbol": "store.py::JobStore.cancel"}]
    assert result.success  # recorded only: no error, no revision round
    assert result.report["revision_rounds"] == 0


def test_facts_without_why_still_parse():
    the_facts = parse_facts(answer(TWO_UNITS))
    assert all(use.why == "" for unit in the_facts.units for use in unit.uses)
