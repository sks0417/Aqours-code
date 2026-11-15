"""Planner v3 with one investigator per draft item (``plan --facts per-item``), no model.

The investigators are fake workers that return scripted answers; Step 1 runs the
real planner entry with a scripted model client, as in test_planner_v3.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

import aqours_code.taskgraph.planner_items as planner_items
from aqours_code.taskgraph import load_graph
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.facts import check_facts
from aqours_code.taskgraph.outline import _read, is_source_file
from aqours_code.taskgraph.outline_levels import (FULL_OUTLINE_PARAGRAPH,
                                                  ITEM_TWO_LEVEL_PARAGRAPH, ItemText,
                                                  build_item_outline, item_texts,
                                                  request_parts, text_words)
from aqours_code.taskgraph.plan_budget import (InvestigatorLimits, PlanBudget, PlanLimits,
                                               SharedBudget)
from aqours_code.taskgraph.planner import PlanOptions, report_path_for, run_plan
from aqours_code.taskgraph.planner_entry import run_planner
from aqours_code.taskgraph.planner_items import (ITEM_LOOK, ITEM_OPENING, ITEM_UNITS, LOOK,
                                                 OPENING, UNITS, YOUR_ITEM_SECTION,
                                                 build_item_prompt, item_instructions,
                                                 merge_answers, read_item_answer)
from aqours_code.taskgraph.planner_v3 import load_facts_prompt, run_resynth
from aqours_code.taskgraph.validate import Issue
from aqours_code.taskgraph.workers import AqoursWorker, WorkerRequest, WorkerResult
from taskgraph_support import git
from test_planner import REQUEST, FakeClient, ScriptedDrafter, answer, final, items_answer, \
    tool_use
from test_planner_v3 import FINAL, ITEMS, TWO_UNITS, logic, plan_v3, toy_like_index, unit

S_UNIT, R_UNIT = TWO_UNITS["units"]
CONVENTION = TWO_UNITS["conventions"][0]
ITEM_S, ITEM_R = ITEMS
GOLDEN = Path(__file__).parent / "golden" / "single_facts_prompt_toy.md"


def facts_answer(*units: dict, conventions=(), together=()) -> str:
    return answer({"conventions": list(conventions), "units": list(units),
                   "together": list(together)})


class FakeInvestigators:
    """A Worker for per-item Step 2: scripted results per item, safe across threads.

    An entry is an answer (a successful run of 3 calls) or a ``WorkerResult``.
    With ``barrier``, every first run waits for the others there, so the test
    fails (BrokenBarrierError) unless they run at the same time. ``traces``
    maps an item to the trace lines its runs write.
    """

    def __init__(self, script: dict[str, list], barrier: threading.Barrier | None = None,
                 traces: dict[str, list[dict]] | None = None):
        self.script = {item: list(entries) for item, entries in script.items()}
        self.barrier, self.traces = barrier, traces or {}
        self.requests: list[WorkerRequest] = []
        self.active = 0
        self.alone: list[tuple[str, int, int]] = []  # (item, attempt, others running)
        self._lock = threading.Lock()

    def describe(self) -> dict:
        return {"worker": "fake", "model": "fake-model"}

    def preflight(self) -> None:
        pass

    def run(self, request: WorkerRequest) -> WorkerResult:
        item = request.node_id.removeprefix("planner-item-")
        with self._lock:
            self.requests.append(request)
            self.alone.append((item, request.attempt, self.active))
            self.active += 1
            entry = self.script[item].pop(0)
        try:
            if self.barrier is not None and request.attempt == 1:
                self.barrier.wait(timeout=20)
            if item in self.traces:
                request.log_dir.mkdir(parents=True, exist_ok=True)
                (request.log_dir / f"trace_{request.attempt}.jsonl").write_text(
                    "\n".join(json.dumps(line) for line in self.traces[item]) + "\n",
                    encoding="utf-8")
            if isinstance(entry, WorkerResult):
                return entry
            return WorkerResult(ok=True, final_answer=entry, model_calls=3,
                                input_tokens=1000, output_tokens=100, duration_s=0.5)
        finally:
            with self._lock:
                self.active -= 1

    def of(self, item: str) -> list[WorkerRequest]:
        return [request for request in self.requests
                if request.node_id == f"planner-item-{item}"]


def plan_items(toy_repo, tmp_path, script: dict[str, list], items=("S", "R"), *,
               worker: FakeInvestigators | None = None, **options):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    worker = worker or FakeInvestigators(script)
    drafter = ScriptedDrafter([items_answer(list(items))])
    plan_options = PlanOptions(request_path=request, repo=toy_repo.path,
                               out=tmp_path / "out" / "graph.json", final_checks=[FINAL],
                               timeout_s=60, facts="per-item", **options)
    return run_plan(plan_options, worker, drafter), worker, plan_options


def ok_script() -> dict[str, list]:
    return {"S": [facts_answer(S_UNIT, conventions=[CONVENTION])],
            "R": [facts_answer(R_UNIT)]}


# ── merging the answers ──

def test_one_investigator_per_item_in_parallel_and_the_answers_merged(toy_repo, tmp_path):
    worker = FakeInvestigators(ok_script(), barrier=threading.Barrier(2))
    result, worker, options = plan_items(toy_repo, tmp_path, {}, worker=worker)
    assert result.success, result.report["errors"]
    (s_request,), (r_request,) = worker.of("S"), worker.of("R")
    # one read-only clone for all; each has its own log directory
    assert s_request.workspace == r_request.workspace
    assert s_request.log_dir != r_request.log_dir
    assert s_request.repo_guard and r_request.repo_guard
    assert "Yours is item S, marked below" in s_request.prompt
    assert "- S: **item S**. achieve S  **(your item)**" in s_request.prompt
    assert "- R: **item R**. achieve R\n" in s_request.prompt
    assert "Yours is item R" in r_request.prompt
    assert s_request.prompt.startswith(item_instructions().rstrip())
    graph = load_graph(options.out)
    assert [node.id for node in graph.nodes] == ["S", "R"]
    assert all(node.goal.endswith(f"- {CONVENTION}") for node in graph.nodes)
    report = result.report
    assert report["facts_mode"] == "per-item" and report["units"] == 2
    assert [unit["id"] for unit in report["facts"]["units"]] == ["S", "R"]
    assert report["items_finished"] == ["S", "R"] and report["items_unfinished"] == []
    # round_usage is in the order the runs ended; investigators is in draft order
    assert sorted(entry.get("item") or "" for entry in report["round_usage"]) == ["", "R", "S"]
    assert [entry["item"] for entry in report["investigators"]] == ["S", "R"]


def test_the_prompt_has_its_sections_in_order(toy_index):
    prompt = build_item_prompt(REQUEST, ITEMS, ITEM_R, "## Level 1\n\n- runner.py", "- README.md")
    order = [prompt.index(heading) for heading in (
        "\n## Your item\n", "\n## Output format\n", "\n# The request\n", "\n# Draft items\n",
        "\n# Repository\n", "\n# Repository outline\n", "\n# Your item\n")]
    assert order == sorted(order)
    assert prompt.rstrip().endswith("Describe item R only: R: **item R**")
    assert "Fix your previous answer" not in prompt


def test_the_instructions_are_the_facts_prompt_adapted_to_one_item():
    text = item_instructions()
    for kept in (ITEM_OPENING, ITEM_LOOK, ITEM_UNITS, ITEM_TWO_LEVEL_PARAGRAPH):
        assert kept in text
    for replaced in (OPENING, LOOK, UNITS, FULL_OUTLINE_PARAGRAPH):
        assert replaced not in text
    assert text.index(YOUR_ITEM_SECTION) < text.index("## Output format")
    assert "do not\ndescribe their work, even where you see it in the code" in text
    assert "You cannot see the other items'\n  answers" in text
    # the facts prompt of single mode is untouched
    single = load_facts_prompt("two-level")
    assert OPENING in single and LOOK in single and UNITS in single
    assert "## Your item" not in single
    # everything but the replaced parts is the facts prompt
    rest = text.replace(YOUR_ITEM_SECTION + "\n\n", "")
    for old, new in ((OPENING, ITEM_OPENING), (LOOK, ITEM_LOOK), (UNITS, ITEM_UNITS)):
        rest = rest.replace(new, old)
    assert rest.replace(ITEM_TWO_LEVEL_PARAGRAPH, FULL_OUTLINE_PARAGRAPH) == load_facts_prompt()


def test_unit_ids_stay_unique_across_items_by_a_fixed_rule():
    s = read_item_answer(facts_answer({**S_UNIT, "id": "A"}), ITEM_S)
    r = read_item_answer(facts_answer(
        {**R_UNIT, "id": "A"}, unit("B", item="R", edits=[logic("models.py", "Job")]),
        together=[{"units": ["A", "B"], "reason": "one feature"}]), ITEM_R)
    merged = merge_answers([s, r])
    assert [unit.id for unit in merged.facts.units] == ["A", "R-A", "B"]
    assert merged.facts.together[0].units == ["R-A", "B"]
    assert merged.unit_items == {"A": "S", "R-A": "R", "B": "R"}
    assert merged.renamed == {"R": [("A", "R-A", "S")]}
    (item, fix), = merged.normalisations
    assert (item, fix.unit, fix.field, fix.value, fix.new_value) == ("R", "R-A", "id", "A", "R-A")
    # the same answers give the same ids
    assert [u.id for u in merge_answers([s, r]).facts.units] == ["A", "R-A", "B"]
    # a renamed id that is taken too gets a number
    taken = read_item_answer(facts_answer({**S_UNIT, "id": "R-A"}, {**R_UNIT, "id": "A",
                                                                     "item": "S"}), ITEM_S)
    again = merge_answers([taken, read_item_answer(facts_answer({**R_UNIT, "id": "A"}),
                                                   ITEM_R)])
    assert [u.id for u in again.facts.units] == ["R-A", "A", "R-A-2"]


def test_an_id_repeated_within_one_answer_is_left_to_f2():
    twice = read_item_answer(facts_answer(
        S_UNIT, {**S_UNIT, "edits": [logic("store.py", "JobStore.add")], "provides": [],
                 "create": [], "tests": []}), ITEM_S)
    merged = merge_answers([twice, read_item_answer(facts_answer(R_UNIT), ITEM_R)])
    assert [unit.id for unit in merged.facts.units] == ["S", "S", "R"]
    issues = check_facts(merged.facts, ITEMS, toy_like_index())
    assert [(issue.code, issue.nodes) for issue in issues] == [("F2", ["S"])]


def test_every_unit_gets_the_investigators_item_and_together_names_own_units():
    answer_text = facts_answer({**R_UNIT, "item": "S"},
                               together=[{"units": ["R", "S"], "reason": "x"}])
    read = read_item_answer(answer_text, ITEM_R)
    assert read.facts.units[0].item == "R"
    assert [(fix.field, fix.value, fix.new_value) for fix in read.normalisations] == [
        ("item", "S", "R")]
    assert [(issue.code, issue.message) for issue in read.errors] == [(
        "F8", "together lists S, which is not one of your units; `together` names only "
              "units of your own item")]


def test_a_new_file_created_by_two_items_becomes_an_edit_of_the_second(toy_repo, tmp_path):
    s = {**S_UNIT, "size": 100, "create": ["tests/test_cancel.py", "jobs_config.py"],
         "provides": ["store.py::JobStore.cancel", "jobs_config.py::load"]}
    r = {**R_UNIT, "size": 80, "create": ["tests/test_skip.py", "jobs_config.py"],
         "provides": ["runner.py::run_loop", "jobs_config.py::dump"]}
    merged = merge_answers([read_item_answer(facts_answer(s), ITEM_S),
                            read_item_answer(facts_answer(r), ITEM_R)])
    first, second = merged.facts.units
    assert first.create == ["tests/test_cancel.py", "jobs_config.py"]
    assert second.create == ["tests/test_skip.py"]
    edit = second.edits[-1]
    # its share of the size: (80 - 30 lines of other edits) / 2 created files
    assert (edit.file, edit.kind, edit.symbols, edit.lines, edit.standalone) == (
        "jobs_config.py", "logic", ["jobs_config.py::dump"], 25, False)
    (item, fix), = merged.normalisations
    assert (item, fix.unit, fix.field, fix.value) == ("R", "R", "create", "jobs_config.py")
    assert "unit S of item S creates it too" in fix.reason
    assert merged.changed == {"R"}
    assert check_facts(merged.facts, ITEMS, toy_like_index()) == []

    result, _, _ = plan_items(toy_repo, tmp_path, {"S": [facts_answer(s)],
                                                   "R": [facts_answer(r)]})
    assert result.success, result.report["errors"]
    assert [entry["field"] for entry in result.report["normalisations"]] == ["create"]
    assert result.report["normalisations"][0]["item"] == "R"


def test_conventions_are_the_union_without_repeats():
    s = read_item_answer(facts_answer(S_UNIT, conventions=["Rule one.", "Rule two"]), ITEM_S)
    r = read_item_answer(facts_answer(R_UNIT, conventions=["rule  ONE", "Rule three."]),
                         ITEM_R)
    assert merge_answers([s, r]).facts.conventions == ["Rule one.", "Rule two", "Rule three."]


# ── revisions per item ──

BAD_R = {**R_UNIT, "edits": [logic("runner.py", "run_loop"), logic("nope.py", "f")]}


def test_a_revision_reruns_only_the_item_with_the_problem(toy_repo, tmp_path):
    script = {"S": [facts_answer(S_UNIT)], "R": [facts_answer(BAD_R), facts_answer(R_UNIT)]}
    result, worker, _ = plan_items(toy_repo, tmp_path, script)
    assert result.success, result.report["errors"]
    assert len(worker.of("S")) == 1 and len(worker.of("R")) == 2
    revised = worker.of("R")[1].prompt
    section = revised[revised.index("# Fix your previous answer"):]
    assert section.startswith("# Fix your previous answer (revision 1 of 2)")
    assert "after putting the answers of all items together" in section
    assert "nope.py" in section and "[F3] R: edits nope.py" in section
    assert "tests/test_cancel.py" not in section  # S's answer is not in it
    assert "## Symbols the other items provide" not in section
    report = result.report
    assert report["revisions_per_item"] == {"S": 0, "R": 1}
    assert report["revision_rounds"] == 1 and report["graph_round"] == 2
    assert list(report["rounds"][0]["problems"]) == ["R"]
    assert report["rounds"][1]["items"] == ["R"]
    assert [(r["item"], r["round"]) for r in report["investigators"]] == [
        ("S", 1), ("R", 1), ("R", 2)]
    assert load_graph(Path(report["out"])).generator.revision_mode == "llm"


def test_an_unprovided_use_is_revised_with_what_the_other_items_provide(toy_repo, tmp_path):
    wrong = {**R_UNIT, "uses": [{"symbol": "store.py::JobStore.cancel_job",
                                 "need": "interface"}]}
    right = {**R_UNIT, "uses": [{"symbol": "store.py::JobStore.cancel", "need": "interface"}]}
    script = {"S": [facts_answer(S_UNIT)], "R": [facts_answer(wrong), facts_answer(right)]}
    result, worker, _ = plan_items(toy_repo, tmp_path, script)
    assert result.success, result.report["errors"]
    section = worker.of("R")[1].prompt.split("# Fix your previous answer")[1]
    assert "uses store.py::JobStore.cancel_job, which no unit provides" in section
    assert ("## Symbols the other items provide" in section
            and "- item S: `store.py::JobStore.cancel`" in section)
    assert len(worker.of("S")) == 1


def test_a_contradiction_of_two_items_goes_back_to_both(toy_repo, tmp_path):
    from test_planner_v3 import CONFLICTING

    bad_s, bad_r = CONFLICTING["units"]
    script = {"S": [facts_answer(bad_s), facts_answer(S_UNIT)],
              "R": [facts_answer(bad_r), facts_answer(R_UNIT)]}
    result, worker, _ = plan_items(toy_repo, tmp_path, script)
    assert result.success, result.report["errors"]
    assert result.report["revision_reasons"] == [{"round": 1, "codes": ["CONFLICT"]}]
    for item in ("S", "R"):
        assert "[CONFLICT] S, R: the facts contradict each other" in worker.of(item)[1].prompt
    assert result.report["revisions_per_item"] == {"S": 1, "R": 1}


def test_a_format_problem_is_revised_first_and_alone(toy_repo, tmp_path):
    script = {"S": [facts_answer(S_UNIT)], "R": ["no json here", facts_answer(R_UNIT)]}
    result, worker, _ = plan_items(toy_repo, tmp_path, script)
    assert result.success
    assert result.report["revision_reasons"] == [{"round": 1, "codes": ["FORMAT"]}]
    assert "Only the format problems below were found" in worker.of("R")[1].prompt
    assert len(worker.of("S")) == 1


def test_renamed_ids_are_shown_to_the_revised_investigator(toy_repo, tmp_path):
    bad = {**BAD_R, "id": "S"}
    script = {"S": [facts_answer(S_UNIT)], "R": [facts_answer(bad), facts_answer(R_UNIT)]}
    result, worker, _ = plan_items(toy_repo, tmp_path, script)
    assert result.success
    section = worker.of("R")[1].prompt.split("# Fix your previous answer")[1]
    assert "The program renamed your unit S to R-S, because item S has a unit S" in section
    assert '"id": "R-S"' in section and "[F3] R-S: edits nope.py" in section


def test_the_last_revision_of_an_item_ends_the_plan(toy_repo, tmp_path):
    script = {"S": [facts_answer(S_UNIT)], "R": [facts_answer(BAD_R)] * 3}
    result, worker, _ = plan_items(toy_repo, tmp_path, script)
    assert not result.success and len(worker.of("R")) == 3 and len(worker.of("S")) == 1
    assert result.report["revisions_per_item"] == {"S": 0, "R": 2}
    assert [issue["code"] for issue in result.report["errors"]] == ["F3"]
    assert not result.report["graph_written"]


def test_a_problem_that_names_no_unit_stops_the_plan(toy_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(planner_items, "check_facts",
                        lambda facts, items, index: [Issue("F9", [], "nobody's problem")])
    result, worker, _ = plan_items(toy_repo, tmp_path, ok_script())
    assert not result.success and len(worker.requests) == 2
    errors = result.report["errors"]
    assert errors[0]["code"] == "UNASSIGNED"
    assert "these problems name no unit" in errors[0]["message"]
    assert "nobody's problem" in errors[0]["message"]
    assert result.report["stopped"]["code"] == "UNASSIGNED"


# ── failures and limits ──

def failed(reason: str, error: str = "it broke") -> WorkerResult:
    return WorkerResult(ok=False, reason=reason, error=error, model_calls=2,
                        input_tokens=500, output_tokens=0, duration_s=0.25)


def test_a_failed_investigator_runs_again_alone_and_may_succeed(toy_repo, tmp_path):
    script = ok_script()
    script["S"].insert(0, failed("worker_error"))
    worker = FakeInvestigators(script, barrier=threading.Barrier(2))
    result, worker, _ = plan_items(toy_repo, tmp_path, {}, worker=worker)
    assert result.success, result.report["errors"]
    assert [request.attempt for request in worker.of("S")] == [1, 2]
    assert ("S", 2, 0) in worker.alone  # nothing else ran beside the second run
    runs = [(r["item"], r["run"], r["rerun"], r["ended"]) for r in result.report["investigators"]]
    assert runs == [("S", 1, False, "worker_error"), ("S", 2, True, "answer"),
                    ("R", 1, False, "answer")]


def test_an_investigator_that_reaches_its_limit_runs_again(toy_repo, tmp_path):
    script = ok_script()
    script["S"].insert(0, failed("model_budget", "the model_calls limit was reached"))
    result, worker, _ = plan_items(toy_repo, tmp_path, script)
    assert result.success
    assert [r["ended"] for r in result.report["investigators"] if r["item"] == "S"] == [
        "model_budget", "answer"]


def test_a_second_failure_stops_the_plan_and_names_the_item(toy_repo, tmp_path):
    script = {"S": [failed("worker_timeout", "killed"), failed("worker_error", "again")],
              "R": [facts_answer(R_UNIT)]}
    result, worker, options = plan_items(toy_repo, tmp_path, script)
    assert not result.success and not options.out.exists()
    (error,) = result.report["errors"]
    assert error["code"] == "AGENT"
    assert error["message"].startswith(
        "planning stopped: the investigator of item S (item S) in step 2 round 1 failed "
        "twice (worker_timeout, then worker_error when run again alone): again.")
    assert "Items with an answer: R; without: S" in error["message"]
    report = result.report
    assert report["stopped"]["item"] == "S" and report["stopped"]["code"] == "AGENT"
    assert report["items_finished"] == ["R"] and report["items_unfinished"] == ["S"]
    # the answer of R is kept
    assert report["rounds"][0]["facts_json"]["R"].strip().startswith("{")
    r_run, = [r for r in report["investigators"] if r["item"] == "R"]
    assert '"run_loop"' in r_run["answer"] or "runner.py::run_loop" in r_run["answer"]


def test_the_plan_limits_cover_all_investigators_together(toy_repo, tmp_path):
    # step 1 uses 1 call; S may use 9 of the 10 and uses 3; R gets the 6 left
    result, worker, _ = plan_items(toy_repo, tmp_path, ok_script(), max_model_calls=10,
                                   max_investigators=1)
    assert result.success
    assert [request.model_budget["max_calls"] for request in worker.requests] == [9, 6]
    assert result.report["used"]["model_calls"] == 7
    assert [r["limits"]["cut_by_plan"] for r in result.report["investigators"]] == [
        ["model_calls"], ["model_calls"]]


def test_investigators_running_at_once_wait_for_what_the_others_free(toy_repo, tmp_path):
    result, worker, _ = plan_items(toy_repo, tmp_path, ok_script(), max_model_calls=10,
                                   max_investigators=2)
    assert result.success
    assert sorted(request.model_budget["max_calls"] for request in worker.requests) == [6, 9]


def test_no_investigator_starts_when_the_plan_limit_is_reached(toy_repo, tmp_path):
    result, worker, _ = plan_items(toy_repo, tmp_path, ok_script(), max_model_calls=1)
    assert not result.success and worker.requests == []
    (error,) = result.report["errors"]
    assert error["code"] == "LIMIT"
    assert error["message"].startswith(
        "planning stopped before the investigator of item S (item S) in step 2 round 1: "
        "the plan's limit of 1 model calls was reached.")
    assert result.report["stopped"]["item"] == "S"


def test_a_failure_without_room_to_run_again_is_a_limit(toy_repo, tmp_path):
    script = {"S": [failed("worker_error")], "R": [facts_answer(R_UNIT)]}
    # step 1: 1 call; S fails after 2 and R uses 3: nothing is left of 6
    result, worker, _ = plan_items(toy_repo, tmp_path, script, max_model_calls=6,
                                   max_investigators=1)
    assert not result.success and len(worker.of("S")) == 1
    (error,) = result.report["errors"]
    assert error["code"] == "LIMIT"
    assert ("the investigator of item S (item S) in step 2 round 1 failed (worker_error), "
            "and the plan's limit of 6 model calls leaves no room to run it again"
            in error["message"])


def test_every_investigator_has_limits_of_its_own(toy_repo, tmp_path):
    result, worker, _ = plan_items(toy_repo, tmp_path, ok_script(),
                                   investigator_max_calls=7, investigator_max_input_tokens=1234,
                                   investigator_max_s=30.0)
    assert result.success
    for request in worker.requests:
        assert request.model_budget == {"max_calls": 7, "max_input_tokens": 1234}
        assert request.timeout_s == 30.0
    assert result.report["investigator_limits"] == {"model_calls": 7, "input_tokens": 1234,
                                                    "seconds": 30.0}
    # no limit at all: no model budget
    result, worker, _ = plan_items(toy_repo, tmp_path, ok_script(), max_model_calls=None,
                                   max_input_tokens=None, investigator_max_calls=None,
                                   investigator_max_input_tokens=None)
    assert all(request.model_budget is None for request in worker.requests)


def test_shared_budget_reserves_frees_and_waits():
    budget = PlanBudget(PlanLimits(10, 1000, None))
    shared = SharedBudget(budget, InvestigatorLimits(6, 800, 100.0))
    first = shared.reserve(60.0)
    assert (first.model_calls, first.input_tokens, first.timeout_s, first.cut_by_plan) == (
        6, 800, 60.0, ())
    second = shared.reserve(60.0)
    assert (second.model_calls, second.input_tokens) == (4, 200)
    assert second.cut_by_plan == ("model_calls", "input_tokens")
    got = []
    waiting = threading.Thread(target=lambda: got.append(shared.reserve(60.0)))
    waiting.start()
    waiting.join(0.2)
    assert waiting.is_alive()  # nothing is free while both run
    shared.release(first, 2, 1, WorkerResult(ok=True, model_calls=2, input_tokens=100), "a")
    waiting.join(5)
    assert (got[0].model_calls, got[0].input_tokens) == (4, 700)
    shared.release(second, 2, 1, WorkerResult(ok=True, model_calls=4, input_tokens=200), "b")
    shared.release(got[0], 2, 1, WorkerResult(ok=True, model_calls=4, input_tokens=100), "c")
    assert shared.reserve(60.0) == "model_calls"  # 10 used, none running
    assert [entry["item"] for entry in budget.rounds] == ["a", "b", "c"]
    timed = SharedBudget(PlanBudget(PlanLimits(None, None, 30.0)), InvestigatorLimits())
    assert timed.reserve(60.0) == "seconds"  # less than a round's minimum is left


# ── the report and the command line ──

TRACE = [{"type": "compact"}, {"type": "tool_use", "tool": "read_file"},
         {"type": "tool_use", "tool": "read_file"}, {"type": "tool_use", "tool": "glob"}]


def test_the_report_has_every_investigator(toy_repo, tmp_path):
    worker = FakeInvestigators(ok_script(), traces={"S": TRACE})
    result, _, _ = plan_items(toy_repo, tmp_path, {}, worker=worker)
    report = json.loads(report_path_for(Path(result.report["out"])).read_text(encoding="utf-8"))
    s_run, r_run = report["investigators"]
    for key in ("item", "title", "round", "run", "rerun", "model_calls", "input_tokens",
                "output_tokens", "duration_s", "compactions", "read_file", "prompt_chars",
                "level2_files", "level2_cut", "limits", "ended", "log_dir"):
        assert key in s_run, key
    assert (s_run["item"], s_run["model_calls"], s_run["input_tokens"],
            s_run["output_tokens"]) == ("S", 3, 1000, 100)
    assert (s_run["compactions"], s_run["read_file"]) == (1, 2)
    assert (r_run["compactions"], r_run["read_file"]) == (None, None)  # it left no trace
    assert s_run["prompt_chars"] == len(worker.of("S")[0].prompt)
    assert s_run["ended"] == "answer"
    assert report["step2_investigators_s"] == 1.0 and report["step2_wall_s"] >= 0
    assert report["revisions_per_item"] == {"S": 0, "R": 0}
    assert report["max_investigators"] == 8
    assert report["investigator_limits"] == {"model_calls": 40, "input_tokens": 1_500_000,
                                             "seconds": 900.0}
    assert [entry["item"] for entry in report["item_summary"]] == ["S", "R"]
    assert report["outline_mode"] == "per-item"
    assert set(report["outline_per_item"]) == {"S", "R"}
    assert report["stage_totals"]["stage2"] == {"model_calls": 6, "input_tokens": 2000,
                                                "output_tokens": 200}


def test_single_mode_sends_the_same_prompt_as_before(toy_repo, tmp_path):
    result, worker, _ = plan_v3(toy_repo, tmp_path, [answer(TWO_UNITS)])
    # the prompt of the code at tag sweevo-single-planner, for the same input
    assert worker.requests[0].prompt == GOLDEN.read_text(encoding="utf-8")
    assert result.report["facts_mode"] == "single"
    assert "investigators" not in result.report
    assert all("item" not in entry for entry in result.report["round_usage"])


def run_plan_cli(toy_repo, tmp_path, monkeypatch, worker, *flags):
    import aqours_code.taskgraph.planner as planner_module

    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = tmp_path / "graph.json"
    drafter = ScriptedDrafter([items_answer(["S", "R"])])
    monkeypatch.setattr(planner_module, "planner_worker", lambda: worker)
    monkeypatch.setattr(planner_module, "draft_worker", lambda: drafter)
    code = main(["plan", str(request), "--repo", str(toy_repo.path), "--out", str(out),
                 "--final-check", FINAL, *flags])
    return code, out


def test_plan_command_prints_one_line_per_investigator(toy_repo, tmp_path, monkeypatch,
                                                       capsys):
    code, out = run_plan_cli(toy_repo, tmp_path, monkeypatch, FakeInvestigators(ok_script()),
                             "--facts", "per-item", "--max-investigators", "2",
                             "--investigator-max-calls", "0")
    assert code == 0
    output = capsys.readouterr().out
    assert ("facts: per item, at most 2 investigators at once; each within - calls, "
            "1500000 input tokens, 900.0 s") in output
    lines = [line for line in output.splitlines() if line.startswith("  investigator ")]
    assert [line.split(":")[0] for line in lines] == [
        "  investigator item S (round 1)", "  investigator item R (round 1)"]
    assert ("3 calls, 1,000 in / 100 out, 0s, - compactions, - read_file, prompt "
            in lines[0])
    assert lines[0].endswith("answer")
    assert "step 2 round" not in output  # the investigators are not listed twice
    assert "revisions per item: S: 0  R: 0" in output
    assert "items with an answer: S, R" in output
    assert load_graph(out).nodes


@pytest.mark.parametrize(("flags", "message"), [
    (["--max-investigators", "2"], "--max-investigators applies only to --facts per-item"),
    (["--investigator-max-time", "5"], "applies only to --facts per-item"),
    (["--facts", "per-item", "--outline", "full"], "cannot use --outline full"),
    (["--facts", "per-item", "--planner", "v2"], "--facts per-item applies only to --planner v3"),
    (["--facts", "per-item", "--max-investigators", "0"], "--max-investigators must be >= 1"),
    (["--facts", "per-item", "--investigator-max-calls", "-1"], "must be >= 0"),
])
def test_plan_command_refuses_per_item_options_that_do_not_fit(toy_repo, tmp_path, monkeypatch,
                                                              capsys, flags, message):
    code, _ = run_plan_cli(toy_repo, tmp_path, monkeypatch, FakeInvestigators({}), *flags)
    assert code == 2 and message in capsys.readouterr().err


def test_resynth_works_on_a_per_item_plan(toy_repo, tmp_path, capsys):
    result, _, options = plan_items(toy_repo, tmp_path, ok_script())
    again = run_resynth(report_path_for(options.out), tmp_path / "again.json")
    assert again.success
    assert [node.id for node in again.graph.nodes] == ["S", "R"]
    assert again.report["facts_mode"] == "per-item"
    assert again.report["outline_mode"] == "per-item"
    assert set(again.report["outline_per_item"]) == {"S", "R"}
    code = main(["resynth", str(options.out), "--out", str(tmp_path / "third.json")])
    assert code == 0
    assert "outlines: one per draft item (2 items" in capsys.readouterr().out


def test_item_prompts_builds_the_prompts_without_a_model(toy_repo, tmp_path, capsys):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    items = tmp_path / "worker_1.json"
    items.write_text(json.dumps({"ok": True, "final_answer": items_answer(["S", "R"])}),
                     encoding="utf-8")
    out = tmp_path / "prompts"
    code = main(["item-prompts", str(request), "--repo", str(toy_repo.path),
                 "--draft-items", str(items), "--out", str(out)])
    assert code == 0
    output = capsys.readouterr().out
    assert "2 investigators (no model is called)" in output
    assert "item S (item S): prompt " in output and "item R (item R): prompt " in output
    assert (out / "item-S.md").read_text(encoding="utf-8").startswith("# Task:")
    summary = json.loads((out / "item_prompts.json").read_text(encoding="utf-8"))
    assert [entry["item"] for entry in summary["items"]] == ["S", "R"]
    assert summary["items"][0]["prompt_chars"] == len(
        (out / "item-S.md").read_text(encoding="utf-8"))


# ── one clone for all investigators ──

def test_investigators_can_share_one_read_only_clone(tmp_path):
    workspace = tmp_path / "clone"
    workspace.mkdir()
    (workspace / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git(workspace, "init", "-q")
    git(workspace, "add", ".")
    git(workspace, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
        "-m", "a")
    traces = []
    for item in ("1", "2"):
        log_dir = tmp_path / "logs" / f"item-{item}"
        config = AqoursWorker().config_for(WorkerRequest(
            node_id=f"planner-item-{item}", attempt=1, prompt="Describe item.",
            workspace=workspace, log_dir=log_dir, timeout_s=60, repo_guard=True))
        config.update(model_provider="scripted", model="scripted-model")
        log_dir.mkdir(parents=True)
        client = FakeClient([tool_use("read_file", "c1", path="a.py"), final("done")])
        result = run_planner(config, model_client=client)
        assert result.ok, result.error
        traces.append(Path(config["trace_path"]))
        assert Path(config["runtime_root"]).is_relative_to(log_dir)
    assert traces[0] != traces[1] and all(trace.is_file() for trace in traces)
    # the agents wrote nothing into the clone they share
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all",
                             "--ignored"], cwd=workspace, capture_output=True, text=True)
    assert status.stdout == ""


# ── the outline of one item ──

def test_request_parts_are_sections_and_the_lines_before_them():
    text = ("Intro line one.\n* version: add remotes\n\n```\ncode block\n```\n"
            "# Title\n\nBody.\n\n## Interface\n\n```python\n# not a heading\n```\n")
    parts = request_parts(text)
    assert [(part.line, part.section) for part in parts] == [
        (1, False), (2, False), (4, False), (7, True), (11, True)]
    assert parts[2].text == "```\ncode block\n```"
    assert parts[4].text == "## Interface\n\n```python\n# not a heading\n```"


def test_text_words_split_identifiers_and_drop_stop_words():
    words = text_words("URLInfo get_missing_deps queries protected the for")
    assert words == {"urlinf", "url", "info", "get_mi", "missin", "dep", "query", "protec"}
    assert text_words("protection Protect") == {"protec"}


def test_item_texts_give_each_item_the_parts_about_it():
    request = ("Release notes:\n* version: add supported remotes\n* gc: refuse without a "
               "specifier\n* checkout: add --summary to completion\n\n## Interface\n\n"
               "### `pkg/version.py`\n\n```python\ndef get_supported_remotes():\n    ...\n```\n")
    texts = item_texts(request, [("1", "Version reports supported remotes"),
                                 ("2", "GC requires specifier"),
                                 ("3", "Checkout completion summary")])
    assert texts["1"].parts == [2, 8]
    assert texts["2"].parts == [3] and texts["3"].parts == [4]
    assert "### `pkg/version.py`" in texts["1"].text and "gc:" not in texts["1"].text
    assert "versio" in texts["1"].words and "gc" in texts["2"].keys


SOURCES = {
    "pkg/__init__.py": "from pkg.core import Engine\n",
    "pkg/core.py": ("from pkg.util import helper\n\n\nclass Engine:\n    rows = 1\n\n"
                    "    def start(self):\n        pass\n\n    def stop_engine(self):\n"
                    "        pass\n" + "".join(f"\n    def method_{n}(self):\n        pass\n"
                                           for n in range(40))),
    "pkg/util.py": "def helper(x):\n    return x\n",
    "pkg/gc.py": "def collect():\n    pass\n",
    "pkg/plugins/a.py": "def run_alpha():\n    pass\n",
    "pkg/plugins/b.py": "def run_beta():\n    pass\n",
    "other/far.py": "from pkg.core import Engine\n\ndef far_away():\n    pass\n",
}


@pytest.fixture
def sources(tmp_path):
    from aqours_code.taskgraph.affected import scan_sources
    from aqours_code.taskgraph.repo_index import RepoIndex

    for relative, content in SOURCES.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(content, encoding="utf-8", newline="\n")
    index = RepoIndex(commit="abc", files=set(SOURCES))
    files = [_read(tmp_path, path) for path in sorted(index.files) if is_source_file(path)]
    return files, scan_sources(SOURCES, index.files)


def item_text(text: str, keys=(), words=()) -> ItemText:
    return ItemText("1", text, keys=sorted(keys), words=sorted(words))


def test_level2_of_an_item_by_its_rules(sources):
    files, imports = sources
    outline = build_item_outline(files, imports, item_text(
        "Stop the `Engine` and the gc of it", keys={"engine", "gc", "stop"},
        words={"gc"}), budget=5000)
    rules = [(entry.path, entry.rule) for entry in outline.chosen]
    # (b) Engine, (d) the file named gc, then (c) the neighbours of these, nearest first
    assert rules[:2] == [("pkg/core.py", "b"), ("pkg/gc.py", "d")]
    assert [path for path, rule in rules if rule == "c"] == [
        "pkg/__init__.py", "pkg/util.py", "other/far.py"]
    assert "### pkg/core.py (" in outline.text


def test_a_large_file_is_shortened_to_the_items_definitions(sources):
    files, imports = sources
    outline = build_item_outline(files, imports, item_text(
        "Fix `Engine` so it can stop", keys={"engine", "stop"}), budget=700)
    assert outline.shortened == ["pkg/core.py"]
    section = outline.text[outline.text.index("### pkg/core.py"):]
    assert "shortened: only the 2 of its 43 definitions" in section
    assert "def stop_engine(self)" in section and "method_1" not in section


def test_level1_collapses_directories_without_level2_files_when_long(sources):
    files, imports = sources
    text = item_text("the `Engine` of `pkg.core`", keys={"engine"})
    full = build_item_outline(files, imports, text)
    assert full.collapsed == [] and "- pkg/plugins/a.py" in full.text
    short = build_item_outline(files, imports, text, collapse_chars=10)
    # other/ stays listed: its one line is shorter than the collapsed line would be
    assert short.collapsed == ["pkg/plugins/"]
    assert "- pkg/plugins/ (2 source files, 4 lines; none in level 2)" in short.text
    assert "- pkg/plugins/a.py" not in short.text and "- other/far.py" in short.text
    # a neighbour outside the directories of the item's files does not keep one open
    assert "other/far.py" not in [entry.path for entry in short.chosen]
    nothing = build_item_outline(files, imports, item_text("nothing at all"),
                                 collapse_chars=10)
    assert nothing.collapsed == [] and "(no file was chosen)" in nothing.text
