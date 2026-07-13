from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot_s20 import (
    agent_loop,
    basic_tools,
    context,
    cron,
    mcp,
    message_bus,
    runtime_state,
    task_system,
    worktree_system,
)
from codepilot_s20.command_executor import LocalCommandExecutor
from evals import run_eval


def tool_block(name: str, data: dict, block_id: str):
    return SimpleNamespace(type="tool_use", name=name, input=data, id=block_id)


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


class OneForbiddenToolClient:
    def __init__(self, tool_name: str, tool_input: dict):
        self.messages = self
        self.calls = 0
        self.tool_name = tool_name
        self.tool_input = tool_input

    def create(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                content=[tool_block(self.tool_name, self.tool_input, "forbidden")],
                stop_reason="tool_use",
            )
        return SimpleNamespace(content=[text_block("done")], stop_reason="end_turn")


class OneBashClient(OneForbiddenToolClient):
    def __init__(self):
        super().__init__(
            "bash", {"command": "echo safe", "run_in_background": True})


def runtime_snapshot():
    return {
        "WORKDIR": runtime_state.WORKDIR,
        "client": runtime_state.client,
        "MODEL_PROVIDER": runtime_state.MODEL_PROVIDER,
        "MODEL": runtime_state.MODEL,
        "PRIMARY_MODEL": runtime_state.PRIMARY_MODEL,
        "COMMAND_EXECUTOR": runtime_state.COMMAND_EXECUTOR,
        "TOOL_POLICY": runtime_state.TOOL_POLICY,
        "CASE_DEADLINE": runtime_state.CASE_DEADLINE,
        "TASKS_DIR": task_system.TASKS_DIR,
        "WORKTREES_DIR": worktree_system.WORKTREES_DIR,
        "DURABLE_PATH": cron.DURABLE_PATH,
        "SKILLS_DIR": runtime_state.SKILLS_DIR,
        "MEMORY_DIR": context.MEMORY_DIR,
        "MAILBOX_DIR": message_bus.MAILBOX_DIR,
    }


def test_docker_eval_policy_exposes_only_workspace_safe_tools(monkeypatch):
    monkeypatch.setattr(mcp, "TOOL_POLICY", run_eval.DOCKER_EVAL_TOOL_POLICY)
    tools, handlers = mcp.assemble_tool_pool()
    names = {tool["name"] for tool in tools}

    assert names == set(run_eval.DOCKER_EVAL_TOOL_POLICY["allowed_tools"])
    assert set(handlers) == names - {"compact"}
    for forbidden in (
        "create_worktree", "remove_worktree", "spawn_teammate",
        "create_task", "schedule_cron", "connect_mcp", "load_skill",
    ):
        assert forbidden not in names
        assert forbidden not in handlers


def test_forbidden_worktree_call_cannot_reach_host_git(tmp_path, monkeypatch):
    monkeypatch.setattr(
        worktree_system,
        "run_git",
        lambda *_a, **_kw: pytest.fail("host git subprocess must not run"),
    )
    trace = tmp_path / "trace.jsonl"
    client = OneForbiddenToolClient("create_worktree", {"name": "escape"})

    result = agent_loop.run_agent_task(
        "try worktree",
        str(tmp_path),
        str(trace),
        model_client=client,
        model_provider="scripted",
        model="scripted",
        command_executor=LocalCommandExecutor(),
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "done"
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    policy = next(event for event in events if event.get("type") == "tool_policy")
    assert "create_worktree" in policy["disabled_tools"]
    assert any("Unknown: create_worktree" in str(event) for event in events)


def test_eval_file_tool_cannot_read_outside_workspace(tmp_path):
    workspace = tmp_path / "agent_workspace"
    workspace.mkdir()
    outside = tmp_path / "host-secret.txt"
    outside.write_text("TOP-SECRET-HOST-DATA", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    client = OneForbiddenToolClient("read_file", {"path": str(outside)})

    agent_loop.run_agent_task(
        "read host secret", str(workspace), str(trace),
        model_client=client, model_provider="scripted", model="scripted",
        command_executor=LocalCommandExecutor(),
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    trace_text = trace.read_text(encoding="utf-8")
    assert "TOP-SECRET-HOST-DATA" not in trace_text
    assert "Path escapes workspace" in trace_text


def test_eval_background_request_runs_synchronously_and_leaves_no_worker(tmp_path):
    class RecordingExecutor(LocalCommandExecutor):
        def __init__(self):
            super().__init__()
            self.commands = []

        def execute(self, command, cwd, timeout):
            self.commands.append((command, Path(cwd), timeout))
            self.command_execution_count += 1
            return {"stdout": "safe", "stderr": "", "timed_out": False}

    executor = RecordingExecutor()
    agent_loop.run_agent_task(
        "try background", str(tmp_path),
        model_client=OneBashClient(), model_provider="scripted", model="scripted",
        command_executor=executor, tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert executor.commands and executor.commands[0][0] == "echo safe"
    assert not any(task.get("status") == "running"
                   for task in agent_loop.background_tasks.values())


def test_workdir_derived_paths_follow_agent_workspace_and_restore(tmp_path, monkeypatch):
    before = runtime_snapshot()
    observed = {}

    def inspect_loop(messages, _context):
        observed.update(runtime_snapshot())
        messages.append({"role": "assistant", "content": [text_block("done")]})

    monkeypatch.setattr(agent_loop, "agent_loop", inspect_loop)
    agent_loop.run_agent_task("inspect", str(tmp_path), command_executor=LocalCommandExecutor())

    assert observed["TASKS_DIR"] == tmp_path / ".tasks"
    assert observed["WORKTREES_DIR"] == tmp_path / ".worktrees"
    assert observed["DURABLE_PATH"] == tmp_path / ".scheduled_tasks.json"
    assert observed["SKILLS_DIR"] == tmp_path / "skills"
    assert observed["MEMORY_DIR"] == tmp_path / ".memory"
    assert observed["MAILBOX_DIR"] == tmp_path / ".mailboxes"
    assert runtime_snapshot() == before


def test_eval_trace_storage_is_outside_container_visible_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "agent_workspace"
    trusted_runtime = tmp_path / "agent_runtime"
    exported_trace = tmp_path / "trace.jsonl"

    def finish_immediately(messages, _context):
        messages.append({"role": "assistant", "content": [text_block("done")]})

    monkeypatch.setattr(agent_loop, "agent_loop", finish_immediately)
    result = agent_loop.run_agent_task(
        "trusted trace", str(workspace), str(exported_trace),
        command_executor=LocalCommandExecutor(),
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
        trace_storage_root=str(trusted_runtime),
    )

    assert not (workspace / ".codepilot").exists()
    assert Path(result["run_dir"]).is_relative_to(trusted_runtime)
    assert exported_trace.exists()
    assert (trusted_runtime / ".codepilot" / "run_index.json").exists()


@pytest.mark.parametrize("failure_point", ["start_run", "record_hook"])
def test_initialization_failure_restores_every_runtime_value(
    tmp_path, monkeypatch, failure_point,
):
    before = runtime_snapshot()

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"{failure_point} failed")

    monkeypatch.setattr(agent_loop, failure_point, fail)
    with pytest.raises(RuntimeError, match="failed"):
        agent_loop.run_agent_task(
            "fail init", str(tmp_path), command_executor=LocalCommandExecutor(),
            tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
        )

    assert runtime_snapshot() == before
    assert basic_tools.WORKDIR == before["WORKDIR"]
    assert basic_tools.COMMAND_EXECUTOR is before["COMMAND_EXECUTOR"]


def test_real_process_timeout_stops_non_bash_tool_loop_and_next_case_runs(tmp_path):
    parent_state = runtime_snapshot()
    workspace = tmp_path / "loop"
    workspace.mkdir()
    config = run_eval.EvalExecutionConfig(backend="local", docker_timeout=1.5)
    started = time.monotonic()
    _run, error, metadata = run_eval._run_isolated_agent_phase(
        task="loop forever without bash",
        case_name="_infinite_non_bash_tool_loop",
        agent_workspace=workspace,
        trace_path=tmp_path / "loop-trace.jsonl",
        stdout_path=tmp_path / "loop-stdout.txt",
        stderr_path=tmp_path / "loop-stderr.txt",
        scripted=True,
        config=config,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert "CaseTimeoutError" in error
    assert metadata["overall_timed_out"] is True
    assert not any(child.name.startswith("codepilot-eval-")
                   for child in multiprocessing.active_children())
    assert runtime_snapshot() == parent_state

    next_workspace = tmp_path / "next"
    next_workspace.mkdir()
    (next_workspace / "info.txt").write_text(
        "Project code: ALPHA-42\nOwner: Eval Systems\nLaunch: September\n",
        encoding="utf-8",
    )
    run_info, next_error, next_metadata = run_eval._run_isolated_agent_phase(
        task="read info",
        case_name="read_file_basic",
        agent_workspace=next_workspace,
        trace_path=tmp_path / "next-trace.jsonl",
        stdout_path=tmp_path / "next-stdout.txt",
        stderr_path=tmp_path / "next-stderr.txt",
        scripted=True,
        config=run_eval.EvalExecutionConfig(backend="local", docker_timeout=8),
    )

    assert next_error == ""
    assert "ALPHA-42" in run_info["final_answer"]
    assert next_metadata["agent_process_exit_code"] == 0


def test_case_exception_result_does_not_claim_unverified_cleanup(tmp_path):
    result = run_eval.case_exception_result(
        tmp_path / "case", tmp_path / "runs", RuntimeError("early failure"),
        run_eval.EvalExecutionConfig(backend="docker"),
    )
    assert result["container_cleanup_succeeded"] is False


def test_model_and_command_timeouts_are_capped_by_remaining_case_time(
    tmp_path, monkeypatch,
):
    observed = {}

    class Messages:
        def create(self, **_kwargs):
            observed["model_timeout"] = float(os.environ["MODEL_REQUEST_TIMEOUT"])
            return SimpleNamespace(content=[text_block("done")], stop_reason="end_turn")

    class Executor:
        def execute(self, _command, _cwd, timeout):
            observed["command_timeout"] = timeout
            return {"stdout": "", "stderr": "", "timed_out": False}

    deadline = time.monotonic() + 0.75
    monkeypatch.setattr(agent_loop, "CASE_DEADLINE", deadline)
    monkeypatch.setattr(agent_loop, "client", SimpleNamespace(messages=Messages()))
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT", "30")
    agent_loop.call_llm([], {}, [], agent_loop.RecoveryState(), 100)

    monkeypatch.setattr(basic_tools, "CASE_DEADLINE", deadline)
    basic_tools.run_bash("echo ok", cwd=tmp_path, timeout=120, executor=Executor())

    assert 0 < observed["model_timeout"] <= 0.75
    assert 0 < observed["command_timeout"] <= 0.75
    assert os.environ["MODEL_REQUEST_TIMEOUT"] == "30"


def test_cleanup_failure_does_not_block_runtime_restoration(tmp_path, monkeypatch):
    before = runtime_snapshot()

    class StopFails(LocalCommandExecutor):
        def stop(self):
            raise RuntimeError("stop cleanup failed")

    def finish_immediately(messages, _context):
        messages.append({"role": "assistant", "content": [text_block("done")]})

    monkeypatch.setattr(agent_loop, "agent_loop", finish_immediately)
    with pytest.raises(RuntimeError, match="stop cleanup failed"):
        agent_loop.run_agent_task(
            "cleanup", str(tmp_path), command_executor=StopFails(),
            tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
        )

    assert runtime_snapshot() == before
