"""``plan --gate`` with scripted models: split, single, a failed gate, limits, the CLI."""
from __future__ import annotations

import json

import pytest

import aqours_code.taskgraph.gate as gate
import aqours_code.taskgraph.planner as planner_module
from aqours_code.taskgraph import build_index, load_graph
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.gate import (COMPACT_TRIGGER_ENV, GATE_VERSION, WHERE_TO_LOOK_LIMIT,
                                        WHERE_TO_LOOK_TITLE, FeatureFiles, where_to_look)
from aqours_code.taskgraph.plan_budget import (DEFAULT_MAX_INPUT_TOKENS, GATE_STEP, PlanBudget,
                                               PlanLimits, round_name)
from aqours_code.taskgraph.planner import PLANNER_VERSION, PlanOptions, report_path_for, run_plan
from aqours_code.taskgraph.planner_v3 import gate_path_for
from aqours_code.taskgraph.single import SINGLE_INSTRUCTION, single_graph
from aqours_code.taskgraph.validate import validate
from aqours_code.taskgraph.workers import WorkerResult
from plan_snapshot import GOLDEN, output_lines
from test_planner import REQUEST, ScriptedDrafter, ScriptedPlanner, answer, items_answer
from test_planner_v3 import FINAL, TWO_UNITS

# S changes store.py and reads models.py; R changes runner.py: nothing shared
LOCATION = answer({"features": [{"id": "S", "modify": ["store.py"], "reference": ["models.py"]},
                                {"id": "R", "modify": ["runner.py"]}]})
# W = round(37000 x 8000 / 80000) = 3700 < M (about 8,200 tokens): the gate splits
SMALL_TRIGGER = "8000"


@pytest.fixture(autouse=True)
def no_compaction_setting(monkeypatch, tmp_path):
    """W from the default trigger unless a test sets one."""
    monkeypatch.delenv(COMPACT_TRIGGER_ENV, raising=False)
    monkeypatch.setattr(gate, "AQOURS_ROOT", tmp_path / "aqours-root")


def run_cli(toy_repo, tmp_path, monkeypatch, capsys, drafter, *flags, step2=(TWO_UNITS,)):
    """``plan --gate`` through the CLI; the drafter makes step 1 and the gate's call."""
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = tmp_path / "out" / "graph.json"
    worker = ScriptedPlanner([answer(facts) for facts in step2])
    monkeypatch.setattr(planner_module, "planner_worker", lambda: worker)
    monkeypatch.setattr(planner_module, "draft_worker", lambda: drafter)
    capsys.readouterr()
    code = main(["plan", str(request), "--repo", str(toy_repo.path), "--out", str(out),
                 "--final-check", FINAL, "--gate", *flags])
    lines = output_lines(capsys.readouterr().out, str(tmp_path))
    report = json.loads(report_path_for(out).read_text(encoding="utf-8"))
    return code, lines, report, out, worker


def drafter_for(location: str = LOCATION, estimate: int = 800) -> ScriptedDrafter:
    return ScriptedDrafter([items_answer(["S", "R"], estimate), location])


# ── split ──

def test_split_plans_on_with_the_same_draft_and_counts_the_gate(toy_repo, tmp_path,
                                                               monkeypatch, capsys):
    monkeypatch.setenv(COMPACT_TRIGGER_ENV, SMALL_TRIGGER)
    drafter = drafter_for()
    code, lines, report, out, worker = run_cli(toy_repo, tmp_path, monkeypatch, capsys,
                                               drafter)
    assert code == 0 and report["success"] and report["nodes"] == 2
    # one step 1, then the gate's call, then step 2
    assert [request.node_id for request in drafter.requests] == ["planner-draft", "gate-locate"]
    assert len(worker.requests) == 1
    # step 2 got the same prompt as without the gate: the same draft
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["plan"]
    assert worker.requests[0].prompt == golden["step2_requests"][0]["prompt"]
    gate_request = drafter.requests[1]
    assert gate_request.model_budget == {"max_calls": 1,
                                         "max_input_tokens": DEFAULT_MAX_INPUT_TOKENS - 100}
    assert gate_request.log_dir == out.with_name("graph.json.logs") / "gate"
    assert gate_request.repo_guard
    field = report["gate"]
    assert (field["decision"], field["zone"], field["ok"]) == ("split", "clear_split", True)
    assert field["W"] == 3700 and field["M"] > 3700
    assert field["window"]["trigger_source"] == "environment"
    assert field["concentration"]["of"] == "1/2"
    assert field["features"] == [
        {"id": "S", "title": "item S", "modify": ["store.py"], "reference": ["models.py"]},
        {"id": "R", "title": "item R", "modify": ["runner.py"], "reference": []}]
    assert field["locate_tokens"] == {"input_tokens": 100, "output_tokens": 10}
    assert field["output"] == str(gate_path_for(out))
    assert report["stage_totals"] == {
        "stage1": {"model_calls": 1, "input_tokens": 100, "output_tokens": 10},
        "gate": {"model_calls": 1, "input_tokens": 100, "output_tokens": 10},
        "stage2": {"model_calls": 1, "input_tokens": 100, "output_tokens": 10}}
    assert report["totals"] == {"model_calls": 3, "input_tokens": 300, "output_tokens": 30}
    assert [(entry["step"], entry["round"]) for entry in report["round_usage"]] == \
        [(1, 1), (GATE_STEP, 1), (2, 1)]
    assert report["gate_rounds"][0]["agent"]["model_calls"] == 1
    saved = json.loads(gate_path_for(out).read_text(encoding="utf-8"))
    assert saved["decision"] == "split" and saved["plan"] == str(out)
    assert saved["draft"]["source"] == "plan step 1"
    assert [line for line in lines if line.startswith(("gate:", "fast path:", "  gate:"))] == [
        f"gate: split (clear_split): {field['reason']}",
        "fast path: replaced by --gate (--fast-path-lines is not used)",
        "  gate: 1 calls, 100 in / 10 out, <t>s, answer"]
    assert lines[0] == "draft items: 2  estimated changed lines: 800"
    assert lines[1].startswith("gate: split (clear_split): M = ")
    assert lines[-1] == "wrote <tmp>/out/graph.json.gate.json"
    assert not any("single-agent graph" in line for line in lines)


# ── single ──

def test_single_writes_the_single_agent_graph_with_the_hints(toy_repo, tmp_path, monkeypatch,
                                                            capsys):
    drafter = drafter_for()
    code, lines, report, out, worker = run_cli(toy_repo, tmp_path, monkeypatch, capsys,
                                               drafter, step2=())
    assert code == 0 and report["success"]
    assert worker.requests == []  # no step 2
    assert [request.node_id for request in drafter.requests] == ["planner-draft", "gate-locate"]
    graph = load_graph(out)
    assert validate(graph, build_index(toy_repo.path, toy_repo.commit)).ok
    # the graph of the single command, but for the goal, the generator and the log
    plain = single_graph(REQUEST, toy_repo.path, [FINAL]).model_dump(mode="json", by_alias=True)
    data = graph.model_dump(mode="json", by_alias=True)
    for key in ("generator", "revision_log"):
        plain.pop(key), data.pop(key)
    goal = data["nodes"][0].pop("goal")
    plain["nodes"][0].pop("goal")
    assert data == plain
    node = graph.nodes[0]
    assert node.id == "solve" and node.edit_set.any_file and node.check.commands == [FINAL]
    assert graph.final_checks == [FINAL]
    assert goal.startswith(REQUEST + "\n\n") and goal.endswith("\n\n" + SINGLE_INSTRUCTION)
    hint = goal[len(REQUEST) + 2:-len(SINGLE_INSTRUCTION) - 2]
    assert hint.startswith(f"## {WHERE_TO_LOOK_TITLE}\n\n") and len(hint) <= WHERE_TO_LOOK_LIMIT
    assert hint.endswith("- item S: modify store.py; read models.py\n"
                         "- item R: modify runner.py")
    assert "not verified" in hint and "check them yourself" in hint
    assert graph.generator.kind == "planner"
    assert graph.generator.planner_version == f"{PLANNER_VERSION}+{GATE_VERSION}"
    assert graph.generator.model == "scripted-model"
    assert [entry.reason for entry in graph.revision_log] == [
        f"gate: single (clear_single): {report['gate']['reason']}"]
    assert report["gate"]["decision"] == "single" and report["nodes"] == 1
    assert report["stage_totals"]["stage2"]["model_calls"] == 0
    assert report["totals"] == {"model_calls": 2, "input_tokens": 200, "output_tokens": 20}
    assert lines[1] == f"gate: single (clear_single): {report['gate']['reason']}"
    assert lines[-1] == "wrote single-agent graph (gate: single)"


def test_the_fast_path_does_not_apply_with_the_gate(toy_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(COMPACT_TRIGGER_ENV, SMALL_TRIGGER)
    drafter = drafter_for(estimate=100)  # the fast path would take 100 < 500 lines
    code, lines, report, out, worker = run_cli(toy_repo, tmp_path, monkeypatch, capsys,
                                               drafter, "--fast-path-lines", "100000")
    assert code == 0 and report["gate"]["decision"] == "split"
    assert len(worker.requests) == 1 and report["nodes"] == 2  # step 2 ran
    assert report["fast_path"] == {
        "taken": False, "estimated_changed_lines": 100, "threshold": None,
        "replaced_by": "gate",
        "note": "plan --gate: the gate decides instead of the fast path; --fast-path-lines "
                "is not used"}
    assert not any(line.startswith("fast path: taken") for line in lines)


def test_a_single_by_the_gate_is_not_the_fast_path(toy_repo, tmp_path, monkeypatch, capsys):
    code, _lines, report, out, _worker = run_cli(toy_repo, tmp_path, monkeypatch, capsys,
                                                 drafter_for(estimate=100), step2=())
    assert code == 0 and not report["fast_path"]["taken"]
    reasons = [entry.reason for entry in load_graph(out).revision_log]
    assert reasons[0].startswith("gate: single") and "fast path" not in reasons[0]


# ── a failed gate ──

class FailingGate(ScriptedDrafter):
    """Step 1 answers; the gate's call fails."""

    def run(self, request):
        if request.node_id == "gate-locate":
            self.requests.append(request)
            return WorkerResult(ok=False, reason="worker_error", error="boom", model_calls=1,
                                input_tokens=70, output_tokens=0)
        return super().run(request)


@pytest.mark.parametrize(("drafter", "flags", "code", "message"), [
    (drafter_for("no json here"), (), "GATE",
     "planning stopped: the gate's answer could not be read (the final answer has no ```json "
     "code block). Without the gate's decision the plan does not go on. So far: 2 model "
     "calls, 200 input and 20 output tokens"),
    (FailingGate([items_answer(["S", "R"])]), (), "GATE",
     "planning stopped: the gate's location call failed (worker_error): boom. Without the "
     "gate's decision the plan does not go on. So far: 2 model calls, 170 input and 10 "
     "output tokens"),
    (drafter_for(), ("--max-calls", "1"), "LIMIT",
     "planning stopped before the gate's location call: the plan's limit of 1 model calls "
     "was reached. So far: 1 model calls, 100 input and 10 output tokens"),
], ids=["unreadable answer", "failed call", "plan limit"])
def test_a_failed_gate_stops_the_plan_with_an_error(toy_repo, tmp_path, monkeypatch, capsys,
                                                    drafter, flags, code, message):
    exit_code, lines, report, out, worker = run_cli(toy_repo, tmp_path, monkeypatch, capsys,
                                                    drafter, *flags, step2=())
    assert exit_code == 1 and not report["success"] and not out.exists()
    assert worker.requests == []  # neither step 2 nor a single graph
    assert [issue["code"] for issue in report["errors"]] == [code]
    assert report["errors"][0]["message"].startswith(message)
    assert report["stopped"]["code"] == code and report["stopped"]["step"] == GATE_STEP
    field = report["gate"]
    assert not field["ok"] and field["decision"] is None
    assert field["errors"] == report["errors"]
    saved = json.loads(gate_path_for(out).read_text(encoding="utf-8"))
    assert saved["errors"] == report["errors"] and saved["decision"] is None
    assert lines[1] == f"gate: no decision ({report['errors'][0]['message']})"
    assert f"[{code}] {report['errors'][0]['message']}" in lines
    assert not any("single-agent graph" in line for line in lines)


def test_no_draft_means_no_gate(toy_repo, tmp_path, monkeypatch, capsys):
    drafter = ScriptedDrafter(["no json", "still no json"])
    code, lines, report, out, _worker = run_cli(toy_repo, tmp_path, monkeypatch, capsys,
                                                drafter, step2=())
    assert code == 1 and not out.exists()
    assert [request.node_id for request in drafter.requests] == ["planner-draft"] * 2
    assert report["gate"] == {"ok": False, "errors": [], "decision": None,
                              "note": "the gate did not run: step 1 gave no draft"}
    assert report["stage_totals"]["gate"]["model_calls"] == 0
    assert "gate: no decision (the gate did not run: step 1 gave no draft)" in lines


# ── options, limits and the hint section ──

def test_the_gate_is_for_planner_v3_only(toy_repo, tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    assert main(["plan", str(request), "--repo", str(toy_repo.path), "--out",
                 str(tmp_path / "g.json"), "--planner", "v2", "--gate"]) == 2
    assert "--gate applies only to --planner v3" in capsys.readouterr().err
    options = PlanOptions(request_path=request, repo=toy_repo.path, out=tmp_path / "g.json",
                          planner="v2", gate=True)
    with pytest.raises(ValueError, match="only to planner v3"):
        run_plan(options, ScriptedPlanner([]))


def test_run_plan_takes_a_separate_gate_worker(toy_repo, tmp_path):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    drafter, locator = ScriptedDrafter([items_answer(["S", "R"])]), ScriptedDrafter([LOCATION])
    options = PlanOptions(request_path=request, repo=toy_repo.path, out=tmp_path / "g.json",
                          final_checks=[FINAL], gate=True)
    result = run_plan(options, ScriptedPlanner([]), drafter, locator)
    assert result.success and result.report["gate"]["decision"] == "single"
    assert [r.node_id for r in drafter.requests] == ["planner-draft"]
    assert [r.node_id for r in locator.requests] == ["gate-locate"]


def test_the_gate_round_has_its_own_name_in_the_plan_limits():
    assert round_name(1, 2) == "step 1 round 2"
    assert round_name(GATE_STEP, 1) == "the gate's location call"
    budget = PlanBudget(PlanLimits(model_calls=0, input_tokens=None, seconds=None))
    assert budget.limit_issue(2, 1).message.startswith(
        "planning stopped before step 2 round 1: the plan's limit of 0 model calls")
    assert budget.limit_issue(GATE_STEP, 1).message.startswith(
        "planning stopped before the gate's location call: the plan's limit of 0 model calls")


def test_the_gate_call_gets_one_call_and_what_is_left_of_the_plan():
    assert gate.locate_model_budget(None) == {"max_calls": 1, "max_input_tokens": None}
    assert gate.locate_model_budget({"max_calls": 9, "max_input_tokens": 500}) == \
        {"max_calls": 1, "max_input_tokens": 500}
    assert gate.locate_model_budget({"max_calls": None, "max_input_tokens": 500}) == \
        {"max_calls": 1, "max_input_tokens": 500}


def test_the_hint_section_lists_files_per_feature_and_is_cut_to_its_limit():
    features = [FeatureFiles(str(number), f"feature {number} with a long title\nover lines",
                             [f"pkg/module_{number}/implementation_{number}.py"],
                             [f"pkg/reference_{number}.py"]) for number in range(40)]
    text = where_to_look(features)
    assert len(text) <= WHERE_TO_LOOK_LIMIT
    assert text.startswith(f"## {WHERE_TO_LOOK_TITLE}\n\n")
    lines = text.splitlines()
    assert lines[4] == ("- feature 0 with a long title over lines: modify "
                        "pkg/module_0/implementation_0.py; read pkg/reference_0.py")
    listed = [line for line in lines if line.startswith("- feature ")]
    assert lines[-1] == (f"- ... {40 - len(listed)} more feature(s) left out to keep this "
                         "section short")
    assert where_to_look([FeatureFiles("1", "lonely")]).endswith("- lonely: no file found")
    short = where_to_look(features[:2])
    assert "left out" not in short and len(short.splitlines()) == 6
