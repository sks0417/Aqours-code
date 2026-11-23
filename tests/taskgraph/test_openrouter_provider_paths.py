"""AQOURS_CODE_OPENROUTER_PROVIDER reaches every taskgraph agent's requests,
the provider that served each call reaches its trace, and every run records
the providers it was pinned to. No model is called: the HTTP request is
captured.

Every agent runs through one of three entries (``worker_entry``,
``planner_entry --stage draft``, ``planner_entry``; see
``test_max_tokens_paths.py``), all ending in the same agent loop, which sends
its requests through the OpenAI-compatible client. The tests here run each
entry with that client and a captured HTTP request, then check what is
recorded in the trace, ``config.json``, the worker configuration, the plan
report and the gate output.
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
from aqours_code.config import parse_openrouter_provider
from aqours_code.model_api import OpenAICompatibleClient
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.coordinator import RunOptions, run_graph
from aqours_code.taskgraph.gate import GateOptions, run_gate
from aqours_code.taskgraph.planner import PLANNER_ENTRY_MODULE, PlanOptions, report_path_for, run_plan
from aqours_code.taskgraph.planner_v3 import gate_path_for
from aqours_code.taskgraph.single import single_graph
from aqours_code.taskgraph.team import TeamOptions, run_team
from aqours_code.taskgraph.workers import (WORKER_ENTRY_MODULE, AqoursWorker, WorkerRequest,
                                           stage_openrouter_provider)
from test_max_tokens_paths import AGENTS, ENTRIES, LOCATION, no_compaction_setting  # noqa: F401
from test_planner import (REQUEST, VALID_DRAFT, ScriptedDrafter, ScriptedPlanner, answer,
                          implement_ids, items_answer)
from test_planner_v3 import FINAL, TWO_UNITS

ROOT = Path(__file__).resolve().parents[2]
OPENROUTER = "https://openrouter.ai/api/v1"
OTHER = "https://example.invalid/v1"
GLM = "z-ai/glm-5.3-flash"
PINNED = {"order": ["z-ai"], "allow_fallbacks": False}


# ── every agent's requests and traces ──


def capture(monkeypatch, served: str | None = "Z.AI") -> list[dict]:
    payloads: list[dict] = []

    class FakeHttpResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            body = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            if served is not None:
                body["provider"] = served
            return json.dumps(body).encode("utf-8")

    def fake_urlopen(request, timeout):
        payloads.append(json.loads(request.data.decode("utf-8")))
        return FakeHttpResponse()

    monkeypatch.setattr("aqours_code.model_api.urllib.request.urlopen", fake_urlopen)
    return payloads


def run_entry(entry: str, tmp_path: Path, monkeypatch, *, providers, served: str | None = "Z.AI",
              base_url: str = OPENROUTER, provider: str = "openai_compatible",
              provider_name: str = "OpenAI-compatible provider", model: str = GLM):
    """Run ``entry`` with the OpenAI-compatible client; the captured payloads, result,
    and the llm_response events of its trace."""
    payloads = capture(monkeypatch, served)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    request = WorkerRequest(node_id="agent", attempt=1, prompt="Answer in one word.",
                            workspace=workspace, log_dir=tmp_path / "log", timeout_s=60)
    cfg = AqoursWorker().config_for(request)
    cfg.update(model_provider=provider, model=model)
    Path(cfg["log_dir"]).mkdir(parents=True, exist_ok=True)
    client = OpenAICompatibleClient("test-key-not-real", base_url, provider_name=provider_name,
                                    openrouter_provider=providers)
    result = ENTRIES[entry](cfg, client)
    trace = Path(cfg["trace_path"])
    events = ([json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line]
              if trace.is_file() else [])
    return payloads, result, [event for event in events if event.get("type") == "llm_response"]


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_every_agent_is_pinned_on_openrouter_and_traces_who_served(agent, tmp_path, monkeypatch):
    payloads, result, replies = run_entry(AGENTS[agent], tmp_path, monkeypatch,
                                          providers=("z-ai",))
    assert result.ok and payloads
    assert all(payload["provider"] == PINNED for payload in payloads)
    # one llm_response per call, each naming the provider that served it
    assert len(replies) == len(payloads)
    assert all(reply["provider"] == "Z.AI" for reply in replies)


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_unset_no_entry_is_pinned(entry, tmp_path, monkeypatch):
    payloads, result, replies = run_entry(entry, tmp_path, monkeypatch, providers=None)
    assert result.ok and payloads
    assert not any("provider" in payload for payload in payloads)
    # who served it is still recorded: it is what the response says
    assert replies and all(reply["provider"] == "Z.AI" for reply in replies)


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_a_reply_that_names_no_provider_is_traced_without_one(entry, tmp_path, monkeypatch):
    payloads, result, replies = run_entry(entry, tmp_path, monkeypatch, providers=("z-ai",),
                                          served=None)
    assert result.ok and payloads
    assert replies and not any("provider" in reply for reply in replies)


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_no_entry_is_pinned_on_another_base_url(entry, tmp_path, monkeypatch):
    payloads, result, _ = run_entry(entry, tmp_path, monkeypatch, providers=("z-ai",),
                                    base_url=OTHER, model="qwen-test")
    assert result.ok and payloads
    assert not any("provider" in payload for payload in payloads)


@pytest.mark.parametrize("entry", sorted(ENTRIES))
@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-flash"])
def test_every_entry_leaves_deepseek_alone(entry, model, tmp_path, monkeypatch):
    runs = [run_entry(entry, tmp_path / str(bool(providers)), monkeypatch, providers=providers,
                      base_url="https://api.deepseek.com", provider="deepseek",
                      provider_name="DeepSeek", model=model, served=None)[0]
            for providers in (None, ("z-ai",))]

    def fields(payloads):  # the messages name each run's own workspace
        return [{key: value for key, value in payload.items() if key != "messages"}
                for payload in payloads]

    assert runs[0] and fields(runs[0]) == fields(runs[1])
    assert not any("provider" in payload for payload in runs[1])


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_an_invalid_value_fails_every_entry_before_any_call(entry, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_ERROR",
                        parse_openrouter_provider({"AQOURS_CODE_OPENROUTER_PROVIDER": "Z.AI"})[1])
    payloads, result, _ = run_entry(entry, tmp_path, monkeypatch, providers=("z-ai",))
    assert payloads == []
    assert not result.ok and result.reason == "worker_error"
    assert ("AQOURS_CODE_OPENROUTER_PROVIDER must be comma-separated OpenRouter provider "
            "slugs such as z-ai (or unset), got 'Z.AI'") in result.error


# ── what is recorded ──


def _describe(module: str, env: dict) -> dict:
    proc = subprocess.run([sys.executable, "-m", module, "--describe"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "AQOURS_CODE_PROVIDER": "openai_compatible",
                               "AQOURS_CODE_BASE_URL": OPENROUTER,
                               "AQOURS_CODE_MODEL": GLM, **env})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", [WORKER_ENTRY_MODULE, PLANNER_ENTRY_MODULE])
def test_describe_reports_the_providers_the_child_is_pinned_to(module):
    unset = _describe(module, {})
    assert "openrouter_provider" in unset and unset["openrouter_provider"] is None
    pinned = {"AQOURS_CODE_OPENROUTER_PROVIDER": "z-ai"}
    assert _describe(module, pinned)["openrouter_provider"] == ["z-ai"]
    assert _describe(module, {"AQOURS_CODE_OPENROUTER_PROVIDER": "Z-AI, novita"})[
        "openrouter_provider"] == ["z-ai", "novita"]
    # not OpenRouter, or DeepSeek: the requests are not pinned
    assert _describe(module, {**pinned, "AQOURS_CODE_BASE_URL": OTHER})[
        "openrouter_provider"] is None
    assert _describe(module, {**pinned, "AQOURS_CODE_PROVIDER": "deepseek",
                              "AQOURS_CODE_BASE_URL": "https://api.deepseek.com",
                              "AQOURS_CODE_MODEL": "deepseek-flash"})["openrouter_provider"] is None
    bad = _describe(module, {"AQOURS_CODE_OPENROUTER_PROVIDER": "z-ai,"})
    assert bad["openrouter_provider"] is None
    assert bad["openrouter_provider_error"].endswith("got 'z-ai,'")


FAKE_ENTRY = '''\
import json, pathlib, sys
args = sys.argv[1:]
if args[0] == "--describe":
    print(json.dumps({"model_provider": "fake", "model": "fake-model",
                      "max_tokens": {"initial": 8000, "escalated": 16000},
                      "reasoning_effort": "high", "openrouter_provider": ["z-ai"]}))
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


def test_the_worker_configuration_records_the_providers(tmp_path):
    worker = fake_worker(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = WorkerRequest(node_id="N", attempt=1, prompt="do it", workspace=workspace,
                            log_dir=tmp_path / "log", timeout_s=30)
    assert worker.config_for(request)["openrouter_provider"] is None  # not described yet
    assert worker.describe()["openrouter_provider"] == ["z-ai"]
    assert worker.run(request).ok
    saved = json.loads((tmp_path / "log" / "worker_1_config.json").read_text(encoding="utf-8"))
    assert saved["openrouter_provider"] == ["z-ai"]


def test_the_run_config_and_each_attempt_record_the_providers(toy_repo, tmp_path):
    graph = single_graph(REQUEST, toy_repo.path, ["git diff --check"])
    result = run_graph(graph, toy_repo.path, fake_worker(tmp_path),
                       RunOptions(out_dir=tmp_path / "runs", workers=1, worker_timeout_s=60))
    saved = json.loads((result.run_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["worker"]["openrouter_provider"] == ["z-ai"]
    assert result.summary["config"]["worker"]["openrouter_provider"] == ["z-ai"]
    attempt = json.loads((result.run_dir / "nodes" / "solve" / "worker_1_config.json")
                         .read_text(encoding="utf-8"))
    assert attempt["openrouter_provider"] == ["z-ai"]


def test_the_team_config_records_the_providers_of_the_lead_and_the_members(toy_repo, tmp_path):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    result = run_team(TeamOptions(request_path=request, repo=toy_repo.path,
                                  out_dir=tmp_path / "runs", workers=1, worker_timeout_s=60,
                                  lead_timeout_s=60),
                      fake_worker(tmp_path), fake_worker(tmp_path))
    saved = json.loads((result.run_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["worker"]["openrouter_provider"] == ["z-ai"]
    assert saved["lead_worker"]["openrouter_provider"] == ["z-ai"]
    lead = json.loads((result.run_dir / "nodes" / "lead_plan" / "worker_1_config.json")
                      .read_text(encoding="utf-8"))
    assert lead["openrouter_provider"] == ["z-ai"]


class Described:
    """A scripted worker that also reports its OpenRouter providers, like AqoursWorker."""

    def __init__(self, providers: list | None):
        self.providers = providers

    def describe(self) -> dict:
        return {**super().describe(), "openrouter_provider": self.providers}


class DescribedPlanner(Described, ScriptedPlanner):
    def __init__(self, answers, providers):
        ScriptedPlanner.__init__(self, answers)
        Described.__init__(self, providers)


class DescribedDrafter(Described, ScriptedDrafter):
    def __init__(self, answers, providers):
        ScriptedDrafter.__init__(self, answers)
        Described.__init__(self, providers)


def write_request(tmp_path: Path) -> Path:
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    return request


@pytest.mark.parametrize("planner", ["v2", "v3"])
@pytest.mark.parametrize("step1, step2", [(["z-ai"], ["z-ai", "novita"]), (None, None)])
def test_the_plan_report_records_the_providers_of_each_step(planner, step1, step2, toy_repo,
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
    assert saved["openrouter_provider"] == result.report["openrouter_provider"] == {
        "step1": step1, "step2": step2}


def test_plan_gate_records_the_providers_of_the_gate_call_too(toy_repo, tmp_path):
    options = PlanOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                          out=tmp_path / "out" / "graph.json", final_checks=[FINAL],
                          timeout_s=60, gate=True)
    result = run_plan(options, DescribedPlanner([answer(TWO_UNITS)], ["z-ai"]),
                      DescribedDrafter([items_answer(["S", "R"])], ["z-ai"]),
                      DescribedDrafter([LOCATION], ["novita"]))
    assert result.report["openrouter_provider"] == {"step1": ["z-ai"], "step2": ["z-ai"],
                                                    "gate": ["novita"]}
    gate_output = json.loads(gate_path_for(options.out).read_text(encoding="utf-8"))
    assert gate_output["openrouter_provider"] == {"step1": ["z-ai"], "locate": ["novita"]}


def test_the_gate_output_records_the_providers_of_its_calls(toy_repo, tmp_path):
    out = tmp_path / "gate" / "toy.json"
    result = run_gate(GateOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                                  out=out, timeout_s=60),
                      DescribedDrafter([LOCATION], None),
                      DescribedDrafter([items_answer(["S", "R"])], ["z-ai"]))
    assert result.data["draft"]["source"] == "step1"
    assert result.data["openrouter_provider"] == {"step1": ["z-ai"], "locate": None}
    assert json.loads(out.read_text(encoding="utf-8"))["openrouter_provider"] == {
        "step1": ["z-ai"], "locate": None}


def test_the_gate_command_records_the_providers(toy_repo, tmp_path, monkeypatch):
    locator = DescribedDrafter([LOCATION], ["z-ai"])
    drafter = DescribedDrafter([items_answer(["S", "R"])], ["z-ai"])
    monkeypatch.setattr(gate, "locate_worker", lambda: locator)
    monkeypatch.setattr(planner_module, "draft_worker", lambda: drafter)
    out = tmp_path / "gate.json"
    assert main(["gate", str(write_request(tmp_path)), "--repo", str(toy_repo.path),
                 "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["openrouter_provider"] == {
        "step1": ["z-ai"], "locate": ["z-ai"]}


def test_stage_openrouter_provider_describes_a_shared_worker_once():
    calls = []

    class Counting:
        def describe(self):
            calls.append(1)
            return {"openrouter_provider": ["z-ai"]}

    shared, plain = Counting(), SimpleNamespace()
    old = SimpleNamespace(describe=lambda: {"model": "m"})  # described before the setting existed
    assert stage_openrouter_provider({"step1": shared, "gate": shared, "other": plain,
                                      "old": old}) == {
        "step1": ["z-ai"], "gate": ["z-ai"], "other": None, "old": None}
    assert len(calls) == 1
