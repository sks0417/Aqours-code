from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot_s20 import agent_loop, background
from codepilot_s20.command_executor import CaseTimeoutError, LocalCommandExecutor
from evals import run_eval


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block():
    return SimpleNamespace(
        type="tool_use",
        name="bash",
        id="background-test",
        input={
            "command": "python -m pytest -q",
            "run_in_background": True,
        },
    )


class ControlledExecutor(LocalCommandExecutor):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, command, cwd, timeout):
        self.command_execution_count += 1
        self.started.set()
        self.release.wait()
        return {
            "command": command,
            "exit_code": 0,
            "stdout": "2 passed",
            "stderr": "",
            "timed_out": False,
            "duration_ms": 1,
        }

    def stop(self):
        self.release.set()


class BackgroundLifecycleClient:
    def __init__(self):
        self.messages = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return SimpleNamespace(content=[tool_block()], stop_reason="tool_use")
        if len(self.calls) == 2:
            return SimpleNamespace(
                content=[text_block("finishing before the test result")],
                stop_reason="end_turn",
            )
        return SimpleNamespace(
            content=[text_block("observed the completed background test")],
            stop_reason="end_turn",
        )


def test_final_answer_waits_past_old_window_and_reinjects_notification(
    tmp_path, monkeypatch,
):
    executor = ControlledExecutor()
    client = BackgroundLifecycleClient()
    observed_waits = []
    wait_entered = threading.Event()
    real_wait = agent_loop.wait_for_background_tasks

    def recording_wait(timeout=None):
        observed_waits.append(timeout)
        wait_entered.set()
        return real_wait(timeout)

    monkeypatch.setattr(agent_loop, "wait_for_background_tasks", recording_wait)
    outcome = {}

    def run():
        try:
            outcome["result"] = agent_loop.run_agent_task(
                "run the tests",
                str(tmp_path / "workspace"),
                model_client=client,
                model_provider="scripted",
                model="scripted",
                command_executor=executor,
                tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
                case_deadline=time.monotonic() + 3,
                cleanup_grace=0.2,
                runtime_root=str(tmp_path / "state"),
                manage_lifecycle=True,
                approval_mode="non_interactive",
            )
        except BaseException as exc:  # surfaced in the test thread below
            outcome["error"] = exc

    run_thread = threading.Thread(target=run, name="background-lifecycle-test")
    run_thread.start()
    assert executor.started.wait(0.5)
    assert wait_entered.wait(0.5)
    assert run_thread.is_alive(), "run returned while its background test was still active"
    executor.release.set()
    run_thread.join(1.5)

    assert not run_thread.is_alive()
    assert "error" not in outcome
    assert outcome["result"]["final_answer"] == (
        "observed the completed background test")
    assert any(timeout is not None and timeout > 2 for timeout in observed_waits)
    assert len(client.calls) == 3
    second_messages = client.calls[1]["messages"]
    assert any(
        "launch a task/subagent just to wait" in str(message.get("content"))
        for message in second_messages
    )
    third_messages = client.calls[2]["messages"]
    assert any(
        "<task_notification>" in str(message.get("content"))
        and "2 passed" in str(message.get("content"))
        for message in third_messages
    )
    assert background.background_workers_alive() is False
    assert not any(
        thread.name.startswith((
            "codepilot-background-", "codepilot-teammate-", "codepilot-s20-cron"))
        for thread in threading.enumerate()
    )


def test_background_task_past_case_deadline_is_structured_timeout_and_stops(
    tmp_path,
):
    executor = ControlledExecutor()
    client = BackgroundLifecycleClient()

    with pytest.raises(
        CaseTimeoutError,
        match="deadline exceeded while waiting for background tasks",
    ):
        agent_loop.run_agent_task(
            "run the tests",
            str(tmp_path),
            model_client=client,
            model_provider="scripted",
            model="scripted",
            command_executor=executor,
            tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
            case_deadline=time.monotonic() + 0.5,
            cleanup_grace=0.3,
            approval_mode="non_interactive",
        )

    assert executor.release.is_set()
    assert background.background_workers_alive() is False
    assert not any(
        thread.name.startswith("codepilot-background-")
        for thread in threading.enumerate()
    )


def test_interactive_loop_returns_while_long_background_task_keeps_running(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    loop_done = threading.Event()

    def long_running_handler(command, run_in_background=False):
        started.set()
        release.wait()
        return f"completed: {command}"

    responses = iter([
        SimpleNamespace(content=[tool_block()], stop_reason="tool_use"),
        SimpleNamespace(
            content=[text_block("The task is still running in the background.")],
            stop_reason="end_turn",
        ),
        SimpleNamespace(
            content=[text_block("Observed the later notification.")],
            stop_reason="end_turn",
        ),
    ])
    monkeypatch.setattr(
        agent_loop, "assemble_tool_pool",
        lambda: ([], {"bash": long_running_handler}),
    )
    monkeypatch.setattr(
        agent_loop, "call_llm",
        lambda _messages, _context, _tools, _state, _max_tokens: next(responses),
    )
    monkeypatch.setattr(agent_loop, "CASE_DEADLINE", None)
    monkeypatch.setattr(agent_loop, "requires_initial_todo", lambda _messages: False)

    messages = [{"role": "user", "content": "start one background task"}]

    def run_loop():
        try:
            agent_loop.agent_loop(messages, {})
        finally:
            loop_done.set()

    loop_thread = threading.Thread(target=run_loop, name="interactive-loop-test")
    loop_thread.start()
    assert started.wait(0.5)
    returned_before_completion = loop_done.wait(0.5)
    worker_was_alive = background.background_workers_alive()
    release.set()
    loop_thread.join(1)
    assert background.wait_for_background_tasks(1)
    background.collect_background_results()

    assert returned_before_completion is True
    assert worker_was_alive is True
    assert not loop_thread.is_alive()
    assert any(
        "still running in the background" in getattr(block, "text", "")
        for message in messages if message.get("role") == "assistant"
        for block in message.get("content", [])
    )


def test_background_notification_preserves_long_test_result_tail():
    block = tool_block()
    output = "pytest session starts\n" + ("collection output\n" * 500)
    output += "\n================ 5 passed in 0.42s ================"

    background.start_background_task(block, {"bash": lambda **_kwargs: output})
    assert background.wait_for_background_tasks(1.0) is True

    notes = background.collect_background_results()

    assert len(notes) == 1
    assert "pytest session starts" in notes[0]
    assert "characters omitted" in notes[0]
    assert "5 passed in 0.42s" in notes[0]
