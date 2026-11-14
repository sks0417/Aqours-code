"""A plan's limits across all rounds, and a failed planner round is not a revision."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from aqours_code.model_budget import model_budget_snapshot
from aqours_code.taskgraph.plan_budget import MIN_ROUND_S, PlanBudget, PlanLimits
from aqours_code.taskgraph.planner import PlanOptions, run_draft_stage, run_plan
from aqours_code.taskgraph.planner_entry import run_planner
from aqours_code.taskgraph.worker_entry import (BudgetedCountingClient, CountingClient,
                                                counting_client)
from aqours_code.taskgraph.workers import AqoursWorker, WorkerRequest, WorkerResult
from test_planner import (REQUEST, FakeClient, ScriptedDrafter, ScriptedPlanner, answer, final,
                          items_answer, tool_use)
from test_planner_v3 import TWO_UNITS, plan_v3


class FailingPlanner(ScriptedPlanner):
    """Step 2 rounds whose agent fails, with the usage the dask round had."""

    def __init__(self, reason: str, error: str):
        super().__init__([])
        self.reason, self.error = reason, error

    def run(self, request):
        self.requests.append(request)
        return WorkerResult(ok=False, reason=self.reason, error=self.error, model_calls=103,
                            input_tokens=4_530_176, output_tokens=96_549, duration_s=1801.2,
                            final_answer=self.error if self.reason == "worker_error" else "")


TLS = "[Error] URLError: <urlopen error _ssl.c:989: The handshake operation timed out>"


def plan_options(toy_repo, tmp_path, **options) -> PlanOptions:
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    return PlanOptions(request_path=request, repo=toy_repo.path,
                       out=tmp_path / "out" / "graph.json", timeout_s=60, **options)


@pytest.mark.parametrize("reason, error", [("worker_error", TLS),
                                           ("worker_timeout", "eval case deadline exceeded")])
def test_a_failed_round_stops_the_plan_without_a_revision(toy_repo, tmp_path, reason, error):
    worker = FailingPlanner(reason, error)
    result = run_plan(plan_options(toy_repo, tmp_path), worker,
                      ScriptedDrafter([items_answer(["S", "R"])]))
    report = result.report
    assert not result.success and len(worker.requests) == 1  # max_revisions is 2
    assert report["revision_rounds"] == 0
    [issue] = report["errors"]
    assert issue["code"] == "AGENT"
    message = issue["message"]
    assert message.startswith(f"planning stopped: the planner agent of step 2 round 1 "
                              f"failed ({reason}): {error}")
    assert "A revision is only for an answer the model gave, so none was started." in message
    assert "So far: 104 model calls, 4,530,276 input and 96,559 output tokens" in message
    assert report["rounds"][0]["errors"] == [issue]  # the error text is not checked as facts
    assert report["stopped"] == {"code": "AGENT", "step": 2, "round": 1, "reason": reason,
                                 "message": message}
    assert [(e["step"], e["round"], e["model_calls"], e["ended"])
            for e in report["round_usage"]] == [(1, 1, 1, "answer"), (2, 1, 103, reason)]
    assert report["used"]["input_tokens"] == 4_530_276


def test_the_command_line_shows_the_stop_and_the_round_usage(toy_repo, tmp_path, capsys,
                                                             monkeypatch):
    from aqours_code.taskgraph import cli, planner

    worker = FailingPlanner("worker_error", TLS)
    monkeypatch.setattr(planner, "planner_worker", lambda: worker)
    monkeypatch.setattr(planner, "draft_worker", lambda: ScriptedDrafter([items_answer(["S"])]))
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    code = cli.main(["plan", str(request), "--repo", str(toy_repo.path),
                     "--out", str(tmp_path / "graph.json"), "--no-fast-path"])
    out = capsys.readouterr().out
    assert code == 1
    assert "  step 2 round 1: 103 calls, 4,530,176 in / 96,549 out, 1801s, worker_error" in out
    assert "[AGENT] planning stopped: the planner agent of step 2 round 1 failed" in out


def test_a_failed_draft_round_is_not_attempted_again(toy_repo, tmp_path):
    class FailingDrafter(ScriptedDrafter):
        def run(self, request):
            self.requests.append(request)
            return WorkerResult(ok=False, reason="worker_error", error=TLS)

    drafter = FailingDrafter([])
    items, rounds, errors = run_draft_stage(REQUEST, [], drafter, toy_repo.path,
                                            tmp_path / "logs", 60)
    assert items is None and len(drafter.requests) == 1  # DRAFT_ATTEMPTS is 2
    assert [issue.code for issue in errors] == ["AGENT"]
    assert "step 1 round 1 failed (worker_error)" in errors[0].message


def test_a_content_error_is_still_revised(toy_repo, tmp_path):
    wrong = copy.deepcopy(TWO_UNITS)
    wrong["units"][0]["edits"][0]["symbols"].append("runner.py::run_loop")  # F3
    result, worker, _ = plan_v3(toy_repo, tmp_path, [answer(wrong), answer(TWO_UNITS)])
    assert result.success and len(worker.requests) == 2
    assert result.report["stopped"] is None
    assert [e["ended"] for e in result.report["round_usage"]] == ["answer"] * 3


def test_no_revision_starts_once_the_call_limit_is_reached(toy_repo, tmp_path):
    wrong = copy.deepcopy(TWO_UNITS)
    wrong["units"][0]["edits"][0]["symbols"].append("runner.py::run_loop")  # F3
    # step 1 and step 2 round 1 use one call each
    result, worker, _ = plan_v3(toy_repo, tmp_path, [answer(wrong), answer(TWO_UNITS)],
                                max_model_calls=2)
    report = result.report
    assert not result.success and len(worker.requests) == 1
    codes = [issue["code"] for issue in report["errors"]]
    assert codes[0] == "LIMIT" and "F3" in codes  # the round's own errors stay
    assert report["errors"][0]["message"].startswith(
        "planning stopped before step 2 round 2: the plan's limit of 2 model calls was "
        "reached. So far: 2 model calls, 200 input and 20 output tokens")
    assert report["errors"][0]["message"].endswith("Raise it with --max-calls.")
    assert report["limits"] == {"model_calls": 2, "input_tokens": 6_000_000, "seconds": 2400.0}


def test_no_round_starts_without_time_for_it(toy_repo, tmp_path):
    result, worker, _ = plan_v3(toy_repo, tmp_path, [], max_plan_s=MIN_ROUND_S - 1)
    assert not result.success and worker.requests == []
    [issue] = result.report["errors"]
    assert issue["code"] == "LIMIT"
    assert issue["message"].startswith("planning stopped before step 1 round 1: the plan's "
                                       f"limit of {MIN_ROUND_S - 1:.0f} s was reached")


def test_each_round_gets_what_is_left(toy_repo, tmp_path):
    result, worker, _ = plan_v3(toy_repo, tmp_path, [answer(TWO_UNITS)],
                                max_model_calls=10, max_input_tokens=1000, max_plan_s=3000)
    assert result.success
    [request] = worker.requests
    assert request.model_budget == {"max_calls": 9, "max_input_tokens": 900}  # step 1 used one
    assert request.timeout_s == 60  # the round timeout, below the time left
    config = AqoursWorker().config_for(request)
    assert config["model_budget"] == {"max_calls": 9, "max_input_tokens": 900}


def test_limits_can_be_turned_off(toy_repo, tmp_path):
    result, worker, _ = plan_v3(toy_repo, tmp_path, [answer(TWO_UNITS)], max_model_calls=None,
                                max_input_tokens=None, max_plan_s=None)
    assert result.success and worker.requests[0].model_budget is None
    assert result.report["limits"] == {"model_calls": None, "input_tokens": None,
                                       "seconds": None}


def test_a_round_cut_by_the_time_limit_reports_the_limit():
    budget = PlanBudget(PlanLimits(None, None, 100.0))
    fields = budget.request_fields(1800)
    assert fields["timeout_s"] <= 100 and fields["model_budget"] is None
    budget.started -= 100  # the round used all the time left
    timed_out = WorkerResult(ok=False, reason="worker_timeout", error="deadline")
    budget.add(2, 1, timed_out)
    issue = budget.failure_issue(2, 1, timed_out)
    assert issue.code == "LIMIT"
    assert "the plan's limit of 100 s was reached before the planner answered" in issue.message
    assert issue.message.endswith("Raise it with --max-time.")


def test_a_round_stopped_by_the_model_budget_reports_the_limit():
    budget = PlanBudget(PlanLimits(None, 1000, None))
    stopped = WorkerResult(ok=False, reason="model_budget", input_tokens=1000,
                           error="the input_tokens limit was reached before the agent answered")
    budget.add(2, 1, stopped)
    issue = budget.failure_issue(2, 1, stopped)
    assert issue.code == "LIMIT"
    assert "the plan's limit of 1,000 input tokens was reached" in issue.message


# ── the planner agent's model budget (worker_entry) ──

def usage(input_tokens: int):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="x")],
                           stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=1))


class Inner:
    def __init__(self, sizes):
        self.sizes = list(sizes)
        self.messages = self

    def create(self, **_kwargs):
        return usage(self.sizes.pop(0))


def test_workers_get_no_budget():
    plain = counting_client(Inner([]), None)
    assert type(plain) is CountingClient
    assert model_budget_snapshot(plain) == {"available": False}
    assert type(counting_client(Inner([]), {"max_calls": None,
                                            "max_input_tokens": None})) is CountingClient


def test_a_call_limit_is_shown_to_the_loop():
    client = counting_client(Inner([10, 10]), {"max_calls": 3, "max_input_tokens": None})
    assert isinstance(client, BudgetedCountingClient)
    assert model_budget_snapshot(client)["remaining_calls"] == 3
    client.messages.create()
    client.messages.create()
    snapshot = model_budget_snapshot(client)
    assert snapshot["remaining_calls"] == 1 and snapshot["used_calls"] == 2
    assert client.ran_out == ""
    client.max_calls = 2
    assert model_budget_snapshot(client)["remaining_calls"] == 0
    assert client.ran_out == "model_calls"


def test_a_token_limit_is_shown_as_the_calls_it_leaves():
    client = BudgetedCountingClient(Inner([100, 200, 300]), max_input_tokens=1000)
    assert model_budget_snapshot(client)["remaining_calls"] > 100  # no request size known yet
    client.messages.create()  # 100 used, 900 left at 100 a call
    assert model_budget_snapshot(client)["remaining_calls"] == 9
    client.messages.create()  # 300 used, 700 left at 200 a call
    assert model_budget_snapshot(client)["remaining_calls"] == 3
    client.messages.create()  # 600 used, 400 left at 300 a call: still one, for the answer
    assert model_budget_snapshot(client)["remaining_calls"] == 1
    client.input_tokens = 1000
    assert model_budget_snapshot(client)["remaining_calls"] == 0
    assert client.ran_out == "input_tokens"


def test_no_call_left_stops_before_the_first_request():
    client = BudgetedCountingClient(Inner([]), max_calls=0)
    snapshot = model_budget_snapshot(client)
    assert snapshot["available"] and snapshot["remaining_calls"] == 0


def planner_config(tmp_path, budget):
    request = WorkerRequest(node_id="planner", attempt=1, prompt="Describe it.",
                            workspace=tmp_path, log_dir=tmp_path / "logs", timeout_s=60,
                            model_budget=budget)
    config = AqoursWorker().config_for(request)
    config.update(model_provider="scripted", model="scripted-model")
    return config


def test_the_last_call_of_the_budget_asks_for_the_answer_without_tools(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    client = FakeClient([tool_use("read_file", "call_1", path="a.py"), final("the answer"),
                         final("never sent")])
    result = run_planner(planner_config(tmp_path, {"max_calls": 2, "max_input_tokens": None}),
                         model_client=client)
    assert result.ok and result.final_answer == "the answer"  # a real answer, checked as usual
    assert result.model_calls == 2
    assert client.tool_names[0] and client.tool_names[1] == []  # tools off for the last call


def test_a_loop_stopped_by_the_budget_is_not_an_answer(tmp_path):
    client = FakeClient([final("never sent")])
    result = run_planner(planner_config(tmp_path, {"max_calls": 0, "max_input_tokens": None}),
                         model_client=client)
    assert not result.ok and result.reason == "model_budget"
    assert result.model_calls == 0 and "model_calls limit" in result.error
