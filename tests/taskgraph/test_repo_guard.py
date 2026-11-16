"""Host-side agents (planner, team lead) read only files of their repository."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aqours_code.taskgraph.planner import PlanOptions, run_plan
from aqours_code.taskgraph.planner_entry import run_planner
from aqours_code.taskgraph.repo_guard import REFUSAL, RepoGuard, outside_reason
from aqours_code.taskgraph.sandbox import SandboxConfig
from aqours_code.taskgraph.team import LEAD_PLAN, LEAD_WRAPUP
from aqours_code.taskgraph.worker_entry import run_worker
from aqours_code.taskgraph.workers import AqoursWorker, WorkerRequest
from test_planner import REQUEST, ScriptedDrafter, ScriptedPlanner, final, items_answer, tool_use
from test_planner import answer as facts_answer
from test_planner_v3 import TWO_UNITS
from test_team import ScriptedWorker, answer, lead, run, task


def link_outside(link: Path, target: Path) -> None:
    """A directory link inside the repository to ``target`` outside it.

    A symlink where the system allows one, else a Windows directory junction
    (no privilege needed), which ``Path.resolve`` follows the same way.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if sys.platform != "win32":
            pytest.skip("cannot create a symlink here")
    import _winapi  # noqa: PLC0415 - CPython on Windows

    _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "secret.txt").write_text("host file\n", encoding="utf-8")
    return root


# ── which paths and patterns are refused ──

@pytest.mark.parametrize("value", ["a.py", "pkg/mod.py", "pkg\\mod.py", "./a.py", "missing.py",
                                   "pkg/", "."])
def test_a_relative_path_inside_is_allowed(repo, value):
    assert outside_reason(value, repo) is None


@pytest.mark.parametrize("value", ["**/*.py", "pkg/*.py", "*.md", "pkg/**", "[ab].py"])
def test_a_relative_pattern_inside_is_allowed(repo, value):
    assert outside_reason(value, repo, pattern=True) is None


@pytest.mark.parametrize("value", [
    "C:/Users/someone/AppData/Local/Programs/Python/x/Lib/site-packages/dask/core.py",
    "C:\\Windows\\win.ini", "/etc/passwd", "\\\\server\\share\\x.py", "~/x.py", "C:a.py",
])
def test_an_absolute_path_is_refused(repo, value):
    assert outside_reason(value, repo) == "absolute"


def test_an_absolute_path_inside_the_repository_is_refused_too(repo):
    assert outside_reason(str(repo / "a.py"), repo) == "absolute"


@pytest.mark.parametrize("value", ["../outside/secret.txt", "pkg/../a.py", "..\\x.py", ".."])
def test_a_parent_component_is_refused(repo, value):
    assert outside_reason(value, repo) == "parent"


@pytest.mark.parametrize("value, why", [
    ("C:/Users/someone/**/site-packages/dask/dataframe/backends.py", "absolute"),
    ("C:/Users/someone/AppData/Local/pip/cache/**/dask*.whl", "absolute"),
    ("/usr/lib/python3*/site-packages/**", "absolute"),
    ("../**/*.py", "parent"),
    ("pkg/../../*", "parent"),
])
def test_an_absolute_or_parent_glob_pattern_is_refused(repo, value, why):
    assert outside_reason(value, repo, pattern=True) == why


def test_a_link_pointing_outside_is_refused(repo, tmp_path):
    link_outside(repo / "link", tmp_path / "outside")
    assert (repo / "link" / "secret.txt").read_text(encoding="utf-8") == "host file\n"
    assert outside_reason("link/secret.txt", repo) == "outside"
    assert outside_reason("link", repo) == "outside"
    assert outside_reason("link/*.txt", repo, pattern=True) == "outside"
    assert outside_reason("link/**", repo, pattern=True) == "outside"


def test_the_guard_checks_only_read_file_and_glob(repo):
    guard = RepoGuard(repo)
    assert guard.check("bash", {"command": "cat /etc/passwd"}) is None
    assert guard.check("read_file", {"path": "/etc/passwd"}) == "absolute"
    assert guard.check("glob", {"pattern": "../*"}) == "parent"
    assert guard.check("glob", {"pattern": "**/*.py"}) is None


def test_the_guard_checks_the_path_of_grep(repo, tmp_path):
    link_outside(repo / "link", tmp_path / "outside")
    guard = RepoGuard(repo)
    assert guard.check("grep", {"pattern": "x"}) is None  # the workspace root
    assert guard.check("grep", {"pattern": "x", "path": "pkg", "glob": "*.py"}) is None
    assert guard.check("grep", {"pattern": "x", "path": "../outside"}) == "parent"
    assert guard.check("grep", {"pattern": "x", "path": str(tmp_path / "outside")}) == "absolute"
    assert guard.check("grep", {"pattern": "x", "path": "C:/Users"}) == "absolute"
    assert guard.check("grep", {"pattern": "x", "path": "link"}) == "outside"


def test_the_planner_greps_inside_and_is_refused_outside(repo, tmp_path):
    link_outside(repo / "link", tmp_path / "outside")
    client = RecordingClient([
        tool_use("grep", "inside", pattern="x = 1"),
        tool_use("grep", "absolute", pattern="host", path=str(tmp_path / "outside")),
        tool_use("grep", "parent", pattern="host", path="../outside"),
        tool_use("grep", "link", pattern="host", path="link"),
        final("the answer"),
    ])
    result = run_planner(planner_config(repo, tmp_path / "logs", repo_guard=True),
                         model_client=client)
    assert result.ok and result.final_answer == "the answer"
    seen = tool_results(client.requests[-1])
    assert seen["inside"] == "pkg/mod.py:1: x = 1"
    for call in ("absolute", "parent", "link"):
        assert seen[call] == REFUSAL, call
    assert "host file" not in " ".join(seen.values())


# ── through the planner agent ──

class RecordingClient:
    """A model client following a script; it keeps the messages of every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[list] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs.get("messages") or [])
        return self.responses.pop(0) if self.responses else final("done")


def tool_results(messages) -> dict[str, str]:
    results = {}
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = block.get("content")
                if isinstance(text, list):
                    text = " ".join(str(part.get("text", "")) for part in text)
                results[block["tool_use_id"]] = str(text)
    return results


def planner_config(workspace: Path, log_dir: Path, *, repo_guard: bool) -> dict:
    request = WorkerRequest(node_id="planner", attempt=1, prompt="Describe it.",
                            workspace=workspace, log_dir=log_dir, timeout_s=60,
                            repo_guard=repo_guard)
    config = AqoursWorker().config_for(request)
    config.update(model_provider="scripted", model="scripted-model")
    return config


def test_the_planner_reads_inside_and_is_refused_outside(repo, tmp_path):
    link_outside(repo / "link", tmp_path / "outside")
    outside = str(tmp_path / "outside" / "secret.txt")
    client = RecordingClient([
        tool_use("read_file", "inside", path="pkg/mod.py"),
        tool_use("glob", "glob_inside", pattern="**/*.py"),
        tool_use("read_file", "absolute", path=outside),
        tool_use("read_file", "parent", path="../outside/secret.txt"),
        tool_use("glob", "glob_absolute",
                 pattern=str(tmp_path).replace("\\", "/") + "/**/secret.txt"),
        tool_use("glob", "glob_parent", pattern="../**/*.txt"),
        tool_use("read_file", "link", path="link/secret.txt"),
        tool_use("glob", "glob_link", pattern="link/*.txt"),
        final("the answer"),
    ])
    result = run_planner(planner_config(repo, tmp_path / "logs", repo_guard=True),
                         model_client=client)
    assert result.ok and result.final_answer == "the answer"
    seen = tool_results(client.requests[-1])
    assert seen["inside"] == "x = 1"
    assert set(seen["glob_inside"].replace("\\", "/").splitlines()) == {"a.py", "pkg/mod.py"}
    for call in ("absolute", "parent", "glob_absolute", "glob_parent", "link", "glob_link"):
        assert seen[call] == REFUSAL, call
    assert "host file" not in " ".join(seen.values())


def test_without_the_guard_the_tools_behave_as_before(repo, tmp_path):
    # what a worker gets: the Aqours tools' own checks only
    client = RecordingClient([
        tool_use("read_file", "absolute_inside", path=str(repo / "a.py")),
        tool_use("read_file", "parent_inside", path="pkg/../a.py"),
        tool_use("read_file", "escape", path="../outside/secret.txt"),
        final("done"),
    ])
    result = run_worker(planner_config(repo, tmp_path / "logs", repo_guard=False),
                        model_client=client)
    assert result.ok
    seen = tool_results(client.requests[-1])
    assert seen["absolute_inside"] == "a = 1" and seen["parent_inside"] == "a = 1"
    assert seen["escape"].startswith("Error: Path escapes workspace")
    assert REFUSAL not in seen.values()


def test_the_guard_is_off_after_its_run(repo, tmp_path):
    run_planner(planner_config(repo, tmp_path / "logs", repo_guard=True),
                model_client=RecordingClient([final("done")]))
    # a later worker in the same process (tests run in-process) is not refused
    client = RecordingClient([tool_use("read_file", "abs", path=str(repo / "a.py")),
                              final("done")])
    run_worker(planner_config(repo, tmp_path / "logs2", repo_guard=False), model_client=client)
    assert tool_results(client.requests[-1])["abs"] == "a = 1"


# ── which agents get it ──

@pytest.mark.parametrize("planner, answer_text", [("v3", facts_answer(TWO_UNITS)),
                                                  ("v2", "no json")])
def test_the_planner_rounds_of_both_steps_get_the_guard(toy_repo, tmp_path, planner,
                                                        answer_text):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    options = PlanOptions(request_path=request, repo=toy_repo.path, planner=planner,
                          out=tmp_path / "graph.json", timeout_s=60, max_revisions=0)
    drafter = ScriptedDrafter([items_answer(["S", "R"])])
    worker = ScriptedPlanner([answer_text])
    run_plan(options, worker, drafter)
    requests = [*drafter.requests, *worker.requests]
    assert [r.node_id for r in requests] == ["planner-draft", "planner"]
    assert all(r.repo_guard for r in requests)


def test_the_team_lead_gets_the_guard_and_teammates_do_not(toy_repo, tmp_path):
    worker = ScriptedWorker({"T1": {"files": {"T1.txt": "1"}, "answer": "T1 is built"},
                             LEAD_WRAPUP: {"answer": "finished"}})
    lead_worker = lead(answer(task("T1")))
    run(toy_repo, tmp_path, lead_worker, worker)
    by_node = {request.node_id: request.repo_guard
               for request in [*lead_worker.requests, *worker.requests]}
    assert by_node == {LEAD_PLAN: True, "T1": False, LEAD_WRAPUP: True}


def test_workers_in_docker_are_unaffected(tmp_path):
    docker = AqoursWorker(sandbox=SandboxConfig(kind="docker", image="img:test"))
    node = WorkerRequest(node_id="N1", attempt=1, prompt="Do it.", workspace=tmp_path,
                         log_dir=tmp_path / "logs", timeout_s=60)
    config = docker.config_for(node)
    assert config["sandbox"]["kind"] == "docker"
    assert config["repo_guard"] is False and config["model_budget"] is None
