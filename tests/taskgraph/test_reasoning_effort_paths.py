"""AQOURS_CODE_REASONING_EFFORT reaches every taskgraph agent's requests, and
every run records the level. No model is called: the HTTP request is captured.

Every agent runs through one of three entries (``worker_entry``,
``planner_entry --stage draft``, ``planner_entry``; see
``test_max_tokens_paths.py``), all ending in the same agent loop, which sends
its requests through the OpenAI-compatible client. The tests here run each
entry with that client and a captured HTTP request, then check what is
recorded in ``config.json``, the worker configuration, the plan report and the
gate output.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import aqours_code.taskgraph.gate as gate
import aqours_code.taskgraph.planner as planner_module
from aqours_code import config
from aqours_code.config import parse_reasoning_effort
from aqours_code.model_api import OpenAICompatibleClient
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.coordinator import RunOptions, run_graph
from aqours_code.taskgraph.gate import GateOptions, run_gate
from aqours_code.taskgraph.planner import PLANNER_ENTRY_MODULE, PlanOptions, report_path_for, run_plan
from aqours_code.taskgraph.planner_v3 import gate_path_for
from aqours_code.taskgraph.single import single_graph
from aqours_code.taskgraph.team import TeamOptions, run_team
from aqours_code.taskgraph.workers import (WORKER_ENTRY_MODULE, AqoursWorker, WorkerRequest,
                                           stage_reasoning_effort)
from test_max_tokens_paths import AGENTS, ENTRIES, LOCATION, no_compaction_setting  # noqa: F401
from test_planner import (REQUEST, VALID_DRAFT, ScriptedDrafter, ScriptedPlanner, answer,
                          implement_ids, items_answer)
from test_planner_v3 import FINAL, TWO_UNITS

ROOT = Path(__file__).resolve().parents[2]
OPENROUTER = "https://openrouter.ai/api/v1"
OTHER = "https://example.invalid/v1"
USAGE = {"prompt_tokens": 1200, "completion_tokens": 900, "total_tokens": 2100,
         "completion_tokens_details": {"reasoning_tokens": 750}}


# ── every agent's requests ──


def capture(monkeypatch) -> list[dict]:
    payloads: list[dict] = []

    class FakeHttpResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "done"},
                                            "finish_reason": "stop"}],
                               "usage": USAGE}).encode("utf-8")

    def fake_urlopen(request, timeout):
        payloads.append(json.loads(request.data.decode("utf-8")))
        return FakeHttpResponse()

    monkeypatch.setattr("aqours_code.model_api.urllib.request.urlopen", fake_urlopen)
    return payloads


def run_entry(entry: str, tmp_path: Path, monkeypatch, *, effort: str | None,
              base_url: str = OPENROUTER, provider: str = "openai_compatible",
              provider_name: str = "OpenAI-compatible provider",
              model: str = "minimax/minimax-m3.1-flash"):
    """Run ``entry`` with the OpenAI-compatible client; the captured payloads, result, config."""
    payloads = capture(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    request = WorkerRequest(node_id="agent", attempt=1, prompt="Answer in one word.",
                            workspace=workspace, log_dir=tmp_path / "log", timeout_s=60)
    cfg = AqoursWorker().config_for(request)
    cfg.update(model_provider=provider, model=model)
    Path(cfg["log_dir"]).mkdir(parents=True, exist_ok=True)
    client = OpenAICompatibleClient("test-key-not-real", base_url, provider_name=provider_name,
                                    reasoning_effort=effort)
    result = ENTRIES[entry](cfg, client)
    return payloads, result, cfg


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_every_agent_asks_the_level_on_openrouter(agent, tmp_path, monkeypatch):
    payloads, result, cfg = run_entry(AGENTS[agent], tmp_path, monkeypatch, effort="high")
    assert result.ok and payloads
    assert all(payload["reasoning"] == {"effort": "high"} for payload in payloads)
    assert not any("reasoning_effort" in payload for payload in payloads)
    # the trace has the reasoning tokens beside the input and output tokens
    events = [json.loads(line) for line in
              Path(cfg["trace_path"]).read_text(encoding="utf-8").splitlines() if line]
    usages = [event["usage"] for event in events if event.get("type") == "llm_response"]
    assert usages and all(usage == {"input_tokens": 1200, "output_tokens": 900,
                                    "total_tokens": 2100, "reasoning_tokens": 750}
                          for usage in usages)


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_every_entry_asks_the_level_of_other_providers(entry, tmp_path, monkeypatch):
    payloads, result, _ = run_entry(entry, tmp_path, monkeypatch, effort="medium", base_url=OTHER,
                                    model="qwen-test")
    assert result.ok and payloads
    assert all(payload["reasoning_effort"] == "medium" and "reasoning" not in payload
               for payload in payloads)


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_unset_no_entry_asks_any_level(entry, tmp_path, monkeypatch):
    payloads, result, _ = run_entry(entry, tmp_path, monkeypatch, effort=None)
    assert result.ok and payloads
    assert not any("reasoning" in payload or "reasoning_effort" in payload
                   or "thinking" in payload for payload in payloads)


@pytest.mark.parametrize("entry", sorted(ENTRIES))
@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-flash"])
def test_every_entry_leaves_deepseek_alone(entry, model, tmp_path, monkeypatch):
    runs = [run_entry(entry, tmp_path / str(effort), monkeypatch, effort=effort,
                      base_url="https://api.deepseek.com", provider="deepseek",
                      provider_name="DeepSeek", model=model)[0]
            for effort in (None, "max")]

    def fields(payloads):  # the messages name each run's own workspace
        return [{key: value for key, value in payload.items() if key != "messages"}
                for payload in payloads]

    assert runs[0] and fields(runs[0]) == fields(runs[1])
    expected = ({"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
                if model == "deepseek-v4-flash" else {})
    assert all({key: payload[key] for key in ("thinking", "reasoning_effort", "reasoning")
                if key in payload} == expected for payload in runs[1])


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_an_invalid_value_fails_every_entry_before_any_call(entry, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REASONING_EFFORT_ERROR",
                        parse_reasoning_effort({"AQOURS_CODE_REASONING_EFFORT": "hgh"})[1])
    payloads, result, _ = run_entry(entry, tmp_path, monkeypatch, effort="high")
    assert payloads == []
    assert not result.ok and result.reason == "worker_error"
    assert ("AQOURS_CODE_REASONING_EFFORT must be one of low, medium, high, xhigh, max "
            "(or unset), got 'hgh'") in result.error


# ── what is recorded ──


def _describe(module: str, env: dict) -> dict:
    proc = subprocess.run([sys.executable, "-m", module, "--describe"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "AQOURS_CODE_PROVIDER": "openai_compatible",
                               "AQOURS_CODE_BASE_URL": OPENROUTER,
                               "AQOURS_CODE_MODEL": "minimax/minimax-m3.1-flash", **env})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", [WORKER_ENTRY_MODULE, PLANNER_ENTRY_MODULE])
def test_describe_reports_the_level_the_child_asks(module):
    unset = _describe(module, {})
    assert "reasoning_effort" in unset and unset["reasoning_effort"] is None
    assert _describe(module, {"AQOURS_CODE_REASONING_EFFORT": "high"})["reasoning_effort"] == "high"
    deepseek = {"AQOURS_CODE_PROVIDER": "deepseek", "AQOURS_CODE_BASE_URL": "https://api.deepseek.com",
                "AQOURS_CODE_REASONING_EFFORT": "max"}
    assert _describe(module, {**deepseek, "AQOURS_CODE_MODEL": "deepseek-flash"})[
        "reasoning_effort"] is None
    assert _describe(module, {**deepseek, "AQOURS_CODE_MODEL": "deepseek-v4-flash"})[
        "reasoning_effort"] == "high"
    bad = _describe(module, {"AQOURS_CODE_REASONING_EFFORT": "hgh"})
    assert bad["reasoning_effort"] is None
    assert bad["reasoning_effort_error"].endswith("got 'hgh'")


FAKE_ENTRY = '''\
import json, pathlib, sys
args = sys.argv[1:]
if args[0] == "--describe":
    print(json.dumps({"model_provider": "fake", "model": "fake-model",
                      "max_tokens": {"initial": 8000, "escalated": 16000},
                      "reasoning_effort": "high"}))
    sys.exit(0)
config = json.loads(pathlib.Path(args[1]).read_text(encoding="utf-8"))
answer = "not a task list" if config["node_id"] == "lead_plan" else "done"
pathlib.Path(config["result_path"]).write_text(json.dumps(
    {"ok": True, "model_calls": 1, "input_tokens": 1, "output_tokens": 1,
     "final_answer": answer}), encoding="utf-8")
'''


def fake_worker(tmp_path: Path) -> AqoursWorker:
    script = tmp_path / "fake_entry.py"
    script.write_text(FAKE_ENTRY, encoding="utf-8")
    return AqoursWorker(entry_command=[sys.executable, str(script)])


def test_the_worker_configuration_records_the_level(tmp_path):
    worker = fake_worker(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = WorkerRequest(node_id="N", attempt=1, prompt="do it", workspace=workspace,
                            log_dir=tmp_path / "log", timeout_s=30)
    assert worker.config_for(request)["reasoning_effort"] is None  # not described yet
    assert worker.describe()["reasoning_effort"] == "high"
    assert worker.run(request).ok
    saved = json.loads((tmp_path / "log" / "worker_1_config.json").read_text(encoding="utf-8"))
    assert saved["reasoning_effort"] == "high"


def test_the_run_config_and_each_attempt_record_the_level(toy_repo, tmp_path):
    graph = single_graph(REQUEST, toy_repo.path, ["git diff --check"])
    result = run_graph(graph, toy_repo.path, fake_worker(tmp_path),
                       RunOptions(out_dir=tmp_path / "runs", workers=1, worker_timeout_s=60))
    saved = json.loads((result.run_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["worker"]["reasoning_effort"] == "high"
    assert result.summary["config"]["worker"]["reasoning_effort"] == "high"
    attempt = json.loads((result.run_dir / "nodes" / "solve" / "worker_1_config.json")
                         .read_text(encoding="utf-8"))
    assert attempt["reasoning_effort"] == "high"


def test_the_team_config_records_the_level_of_the_lead_and_the_members(toy_repo, tmp_path):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    result = run_team(TeamOptions(request_path=request, repo=toy_repo.path,
                                  out_dir=tmp_path / "runs", workers=1, worker_timeout_s=60,
                                  lead_timeout_s=60),
                      fake_worker(tmp_path), fake_worker(tmp_path))
    saved = json.loads((result.run_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["worker"]["reasoning_effort"] == saved["lead_worker"]["reasoning_effort"] == "high"
    lead = json.loads((result.run_dir / "nodes" / "lead_plan" / "worker_1_config.json")
                      .read_text(encoding="utf-8"))
    assert lead["reasoning_effort"] == "high"


class Described:
    """A scripted worker that also reports its thinking level, like AqoursWorker."""

    def __init__(self, effort: str | None):
        self.effort = effort

    def describe(self) -> dict:
        return {**super().describe(), "reasoning_effort": self.effort}


class DescribedPlanner(Described, ScriptedPlanner):
    def __init__(self, answers, effort):
        ScriptedPlanner.__init__(self, answers)
        Described.__init__(self, effort)


class DescribedDrafter(Described, ScriptedDrafter):
    def __init__(self, answers, effort):
        ScriptedDrafter.__init__(self, answers)
        Described.__init__(self, effort)


def write_request(tmp_path: Path) -> Path:
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    return request


@pytest.mark.parametrize("planner", ["v2", "v3"])
@pytest.mark.parametrize("step1, step2", [("high", "low"), (None, None)])
def test_the_plan_report_records_the_level_of_each_step(planner, step1, step2, toy_repo,
                                                        tmp_path):
    options = PlanOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                          planner=planner, out=tmp_path / "out" / "graph.json",
                          final_checks=[FINAL], timeout_s=60)
    answer2 = answer(TWO_UNITS) if planner == "v3" else answer(VALID_DRAFT)
    items = ["S", "R"] if planner == "v3" else implement_ids([answer2])
    result = run_plan(options, DescribedPlanner([answer2], step2),
                      DescribedDrafter([items_answer(items)], step1))
    assert result.success, result.report["errors"]
    saved = json.loads(report_path_for(options.out).read_text(encoding="utf-8"))
    assert saved["reasoning_effort"] == result.report["reasoning_effort"] == {
        "step1": step1, "step2": step2}


def test_plan_gate_records_the_level_of_the_gate_call_too(toy_repo, tmp_path):
    options = PlanOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                          out=tmp_path / "out" / "graph.json", final_checks=[FINAL],
                          timeout_s=60, gate=True)
    result = run_plan(options, DescribedPlanner([answer(TWO_UNITS)], "low"),
                      DescribedDrafter([items_answer(["S", "R"])], "high"),
                      DescribedDrafter([LOCATION], "medium"))
    assert result.report["reasoning_effort"] == {"step1": "high", "step2": "low", "gate": "medium"}
    gate_output = json.loads(gate_path_for(options.out).read_text(encoding="utf-8"))
    assert gate_output["reasoning_effort"] == {"step1": "high", "locate": "medium"}


def test_the_gate_output_records_the_level_of_its_calls(toy_repo, tmp_path):
    out = tmp_path / "gate" / "toy.json"
    result = run_gate(GateOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                                  out=out, timeout_s=60),
                      DescribedDrafter([LOCATION], None),
                      DescribedDrafter([items_answer(["S", "R"])], "high"))
    assert result.data["draft"]["source"] == "step1"
    assert result.data["reasoning_effort"] == {"step1": "high", "locate": None}
    assert json.loads(out.read_text(encoding="utf-8"))["reasoning_effort"] == {
        "step1": "high", "locate": None}


def test_the_gate_command_records_the_level(toy_repo, tmp_path, monkeypatch):
    locator = DescribedDrafter([LOCATION], "high")
    drafter = DescribedDrafter([items_answer(["S", "R"])], "high")
    monkeypatch.setattr(gate, "locate_worker", lambda: locator)
    monkeypatch.setattr(planner_module, "draft_worker", lambda: drafter)
    out = tmp_path / "gate.json"
    assert main(["gate", str(write_request(tmp_path)), "--repo", str(toy_repo.path),
                 "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["reasoning_effort"] == {
        "step1": "high", "locate": "high"}


def test_stage_reasoning_effort_describes_a_shared_worker_once():
    calls = []

    class Counting:
        def describe(self):
            calls.append(1)
            return {"reasoning_effort": "high"}

    shared, plain = Counting(), SimpleNamespace()
    old = SimpleNamespace(describe=lambda: {"model": "m"})  # described before the setting existed
    assert stage_reasoning_effort({"step1": shared, "gate": shared, "other": plain,
                                   "old": old}) == {
        "step1": "high", "gate": "high", "other": None, "old": None}
    assert len(calls) == 1
