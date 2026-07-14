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
