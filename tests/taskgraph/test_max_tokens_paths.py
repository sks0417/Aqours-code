"""The output limits (AQOURS_CODE_MAX_TOKENS, AQOURS_CODE_ESCALATED_MAX_TOKENS)
reach every taskgraph agent, and every run records them. No model is called.

Every agent runs in a child process through one of three entries, all of
which end in the same agent loop (``run_worker`` -> ``run_agent_task``):

- ``worker_entry``: graph nodes of ``run`` and ``single``, team members, and
  the team lead's wrap-up;
- ``planner_entry --stage draft``: planner step 1, and the gate's location call;
- ``planner_entry`` (stage ground): planner step 2, and the team lead's plan.

The entries run here in process with a scripted model client; the tests
check which entry each agent uses, and what is recorded in ``config.json``,
the worker configuration, the plan report and the gate output.
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
from aqours_code.config import parse_max_tokens
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.coordinator import RunOptions, run_graph
from aqours_code.taskgraph.gate import COMPACT_TRIGGER_ENV, GateOptions, run_gate
from aqours_code.taskgraph.planner import (PLANNER_ENTRY_MODULE, PlanOptions, report_path_for,
                                           run_plan)
from aqours_code.taskgraph.planner_entry import run_planner
from aqours_code.taskgraph.planner_v3 import gate_path_for
from aqours_code.taskgraph.single import single_graph
from aqours_code.taskgraph.team import TeamOptions, run_team
from aqours_code.taskgraph.worker_entry import run_worker
from aqours_code.taskgraph.workers import (WORKER_ENTRY_MODULE, AqoursWorker, WorkerRequest,
                                           stage_max_tokens)
from test_planner import (REQUEST, VALID_DRAFT, ScriptedDrafter, ScriptedPlanner, answer,
                          implement_ids, items_answer)
from test_planner_v3 import FINAL, TWO_UNITS

ROOT = Path(__file__).resolve().parents[2]
CONFIGURED = (50_000, 90_000)
LIMITS = {"initial": 50_000, "escalated": 90_000}
LOCATION = answer({"features": [{"id": "S", "modify": ["store.py"], "reference": ["models.py"]},
                                {"id": "R", "modify": ["runner.py"]}]})


@pytest.fixture(autouse=True)
def no_compaction_setting(monkeypatch, tmp_path):
    """The gate's W from the default trigger (as in test_gate.py)."""
    monkeypatch.delenv(COMPACT_TRIGGER_ENV, raising=False)
    monkeypatch.setattr(gate, "AQOURS_ROOT", tmp_path / "aqours-root")


# ── a scripted model that records the limit of each call ──


def reply(stop_reason: str, text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class LimitClient:
    """First a reply cut at the limit, then an answer; records ``max_tokens``."""

    def __init__(self):
        self.responses = [reply("max_tokens", "still thinking"), reply("end_turn", "done")]
        self.limits: list[int] = []
        self.messages = self

    def create(self, **kwargs):
        self.limits.append(kwargs["max_tokens"])
        return self.responses.pop(0) if self.responses else reply("end_turn", "done")


ENTRIES = {
    "worker_entry": lambda cfg, client: run_worker(cfg, model_client=client),
    "planner_entry --stage draft": lambda cfg, client: run_planner(cfg, client, stage="draft"),
    "planner_entry": lambda cfg, client: run_planner(cfg, client, stage="ground"),
}
# every agent of item 3 and the entry its child process runs
AGENTS = {
    "graph node of run / single": "worker_entry",
    "team member": "worker_entry",
    "team lead wrap-up": "worker_entry",
    "planner step 1 (draft)": "planner_entry --stage draft",
    "gate location call": "planner_entry --stage draft",
    "planner step 2": "planner_entry",
    "team lead plan": "planner_entry",
}


def run_entry(entry: str, tmp_path: Path, provider: str = "scripted",
              model: str = "scripted-model"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    request = WorkerRequest(node_id="agent", attempt=1, prompt="Answer in one word.",
                            workspace=workspace, log_dir=tmp_path / "log", timeout_s=60)
    cfg = AqoursWorker().config_for(request)
    cfg.update(model_provider=provider, model=model)
    Path(cfg["log_dir"]).mkdir(parents=True, exist_ok=True)
    client = LimitClient()
    result = ENTRIES[entry](cfg, client)
    return client.limits, result


def configure(monkeypatch, initial: int, escalated: int) -> None:
    """What AQOURS_CODE_MAX_TOKENS / _ESCALATED_ set in config at import."""
    monkeypatch.setattr(config, "DEFAULT_MAX_TOKENS", initial)
    monkeypatch.setattr(config, "ESCALATED_MAX_TOKENS", escalated)


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_every_agent_keeps_the_default_limits(agent, tmp_path):
    limits, result = run_entry(AGENTS[agent], tmp_path)
    assert limits == [8000, 16000] and result.ok


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_every_agent_uses_the_configured_limits(agent, tmp_path, monkeypatch):
    configure(monkeypatch, *CONFIGURED)
    limits, result = run_entry(AGENTS[agent], tmp_path)
    assert limits == list(CONFIGURED) and result.ok


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_every_agent_keeps_deepseek_at_128000(agent, tmp_path, monkeypatch):
    configure(monkeypatch, *CONFIGURED)
    limits, _ = run_entry(AGENTS[agent], tmp_path, provider="deepseek", model="deepseek-flash")
    assert limits == [128_000]  # no retry: the escalated limit is not higher


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_an_invalid_setting_fails_every_agent_before_any_call(entry, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_TOKENS_ERROR",
                        parse_max_tokens({"AQOURS_CODE_MAX_TOKENS": "lots"})[2])
    limits, result = run_entry(entry, tmp_path)
    assert limits == []
    assert not result.ok and result.reason == "worker_error"
    assert "AQOURS_CODE_MAX_TOKENS must be a positive whole number, got 'lots'" in result.error


def _entry_of(worker: AqoursWorker) -> str:
    command = worker.entry_command
    assert command[:2] == [sys.executable, "-m"]
    return " ".join(command[2:]).replace(WORKER_ENTRY_MODULE, "worker_entry").replace(
        PLANNER_ENTRY_MODULE, "planner_entry")


def test_each_agent_runs_the_entry_listed_above(toy_repo, tmp_path, monkeypatch):
    assert _entry_of(AqoursWorker()) == AGENTS["graph node of run / single"]
    assert _entry_of(planner_module.draft_worker()) == AGENTS["planner step 1 (draft)"]
    assert _entry_of(planner_module.planner_worker()) == AGENTS["planner step 2"]
    assert _entry_of(gate.locate_worker()) == AGENTS["gate location call"]
    # the team command: members and the wrap-up use its worker, the lead's plan its lead_worker
    import aqours_code.taskgraph.team as team_module
    captured = {}

    def capture(options, worker, lead_worker):
        captured.update(worker=worker, lead_worker=lead_worker)
        raise RuntimeError("captured")

    monkeypatch.setattr(team_module, "run_team", capture)
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    assert main(["team", str(request), "--repo", str(toy_repo.path), "--sandbox", "none"]) == 2
    assert _entry_of(captured["worker"]) == AGENTS["team member"] == AGENTS["team lead wrap-up"]
    assert _entry_of(captured["lead_worker"]) == AGENTS["team lead plan"]


# ── what is recorded ──


def _describe(module: str, env: dict) -> dict:
    proc = subprocess.run([sys.executable, "-m", module, "--describe"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "AQOURS_CODE_PROVIDER": "openai_compatible",
                               "AQOURS_CODE_MODEL": "minimax/minimax-m3.1-flash", **env})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", [WORKER_ENTRY_MODULE, PLANNER_ENTRY_MODULE])
def test_describe_reports_the_limits_the_child_uses(module):
    assert _describe(module, {})["max_tokens"] == {"initial": 8000, "escalated": 16000}
    env = {"AQOURS_CODE_MAX_TOKENS": "128000", "AQOURS_CODE_ESCALATED_MAX_TOKENS": "128000"}
    info = _describe(module, env)
    assert info["model"] == "minimax/minimax-m3.1-flash"
    assert info["max_tokens"] == {"initial": 128000, "escalated": 128000}
    deepseek = _describe(module, {**env, "AQOURS_CODE_PROVIDER": "deepseek",
                                  "AQOURS_CODE_MODEL": "deepseek-v4-flash"})
    assert deepseek["max_tokens"] == {"initial": 128000, "escalated": 128000}
    bad = _describe(module, {"AQOURS_CODE_MAX_TOKENS": "0"})
    assert bad["max_tokens"] is None
    assert bad["max_tokens_error"] == "AQOURS_CODE_MAX_TOKENS must be a positive whole number, got '0'"


FAKE_ENTRY = '''\
import json, pathlib, sys
args = sys.argv[1:]
if args[0] == "--describe":
    print(json.dumps({"model_provider": "fake", "model": "fake-model",
                      "max_tokens": {"initial": 50000, "escalated": 90000}}))
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


def test_the_worker_configuration_records_the_described_limits(tmp_path):
    worker = fake_worker(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = WorkerRequest(node_id="N", attempt=1, prompt="do it", workspace=workspace,
                            log_dir=tmp_path / "log", timeout_s=30)
    assert worker.config_for(request)["max_tokens"] is None  # not described yet
    assert worker.describe()["max_tokens"] == LIMITS
    assert worker.describe() == worker.describe()  # the child is asked once
    assert worker.run(request).ok
    saved = json.loads((tmp_path / "log" / "worker_1_config.json").read_text(encoding="utf-8"))
    assert saved["max_tokens"] == LIMITS


def test_the_run_config_and_each_attempt_record_the_limits(toy_repo, tmp_path):
    graph = single_graph(REQUEST, toy_repo.path, ["git diff --check"])
    result = run_graph(graph, toy_repo.path, fake_worker(tmp_path),
                       RunOptions(out_dir=tmp_path / "runs", workers=1, worker_timeout_s=60))
    saved = json.loads((result.run_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["worker"]["max_tokens"] == LIMITS
    assert result.summary["config"]["worker"]["max_tokens"] == LIMITS
    attempt = json.loads((result.run_dir / "nodes" / "solve" / "worker_1_config.json")
                         .read_text(encoding="utf-8"))
    assert attempt["max_tokens"] == LIMITS


def test_the_team_config_records_the_limits_of_the_lead_and_the_members(toy_repo, tmp_path):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    result = run_team(TeamOptions(request_path=request, repo=toy_repo.path,
                                  out_dir=tmp_path / "runs", workers=1, worker_timeout_s=60,
                                  lead_timeout_s=60),
                      fake_worker(tmp_path), fake_worker(tmp_path))
    saved = json.loads((result.run_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["worker"]["max_tokens"] == saved["lead_worker"]["max_tokens"] == LIMITS
    lead = json.loads((result.run_dir / "nodes" / "lead_plan" / "worker_1_config.json")
                      .read_text(encoding="utf-8"))
    assert lead["max_tokens"] == LIMITS


class Described:
    """A scripted worker that also reports output limits, like AqoursWorker."""

    def __init__(self, limits: dict):
        self.limits = limits

    def describe(self) -> dict:
        return {**super().describe(), "max_tokens": self.limits}


class DescribedPlanner(Described, ScriptedPlanner):
    def __init__(self, answers, limits):
        ScriptedPlanner.__init__(self, answers)
        Described.__init__(self, limits)


class DescribedDrafter(Described, ScriptedDrafter):
    def __init__(self, answers, limits):
        ScriptedDrafter.__init__(self, answers)
        Described.__init__(self, limits)


STEP1 = {"initial": 50_000, "escalated": 90_000}
STEP2 = {"initial": 60_000, "escalated": 100_000}
LOCATE = {"initial": 70_000, "escalated": 110_000}


def write_request(tmp_path: Path) -> Path:
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    return request


@pytest.mark.parametrize("planner", ["v2", "v3"])
def test_the_plan_report_records_the_limits_of_each_step(planner, toy_repo, tmp_path):
    options = PlanOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                          planner=planner, out=tmp_path / "out" / "graph.json",
                          final_checks=[FINAL], timeout_s=60)
    step2 = answer(TWO_UNITS) if planner == "v3" else answer(VALID_DRAFT)
    items = ["S", "R"] if planner == "v3" else implement_ids([step2])
    worker = DescribedPlanner([step2], STEP2)
    drafter = DescribedDrafter([items_answer(items)], STEP1)
    result = run_plan(options, worker, drafter)
    assert result.success, result.report["errors"]
    saved = json.loads(report_path_for(options.out).read_text(encoding="utf-8"))
    assert saved["max_tokens"] == result.report["max_tokens"] == {"step1": STEP1, "step2": STEP2}


def test_plan_gate_records_the_limits_of_the_gate_call_too(toy_repo, tmp_path):
    options = PlanOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                          out=tmp_path / "out" / "graph.json", final_checks=[FINAL],
                          timeout_s=60, gate=True)
    result = run_plan(options, DescribedPlanner([answer(TWO_UNITS)], STEP2),
                      DescribedDrafter([items_answer(["S", "R"])], STEP1),
                      DescribedDrafter([LOCATION], LOCATE))
    assert result.report["max_tokens"] == {"step1": STEP1, "step2": STEP2, "gate": LOCATE}
    gate_output = json.loads(gate_path_for(options.out).read_text(encoding="utf-8"))
    assert gate_output["max_tokens"] == {"step1": STEP1, "locate": LOCATE}


def test_the_gate_output_records_the_limits_of_its_calls(toy_repo, tmp_path):
    out = tmp_path / "gate" / "toy.json"
    result = run_gate(GateOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                                  out=out, timeout_s=60),
                      DescribedDrafter([LOCATION], LOCATE),
                      DescribedDrafter([items_answer(["S", "R"])], STEP1))
    assert result.data["draft"]["source"] == "step1"
    assert result.data["max_tokens"] == {"step1": STEP1, "locate": LOCATE}
    assert json.loads(out.read_text(encoding="utf-8"))["max_tokens"] == result.data["max_tokens"]


def test_the_gate_command_records_the_limits(toy_repo, tmp_path, monkeypatch):
    locator = DescribedDrafter([LOCATION], LOCATE)
    drafter = DescribedDrafter([items_answer(["S", "R"])], STEP1)
    monkeypatch.setattr(gate, "locate_worker", lambda: locator)
    monkeypatch.setattr(planner_module, "draft_worker", lambda: drafter)
    out = tmp_path / "gate.json"
    assert main(["gate", str(write_request(tmp_path)), "--repo", str(toy_repo.path),
                 "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["max_tokens"] == {"step1": STEP1,
                                                                        "locate": LOCATE}


def test_stage_max_tokens_describes_a_shared_worker_once():
    calls = []

    class Counting:
        def describe(self):
            calls.append(1)
            return {"max_tokens": STEP1}

    shared, plain = Counting(), SimpleNamespace()
    assert stage_max_tokens({"step1": shared, "gate": shared, "other": plain}) == {
        "step1": STEP1, "gate": STEP1, "other": None}
    assert len(calls) == 1
