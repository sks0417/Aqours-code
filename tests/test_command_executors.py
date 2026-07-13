from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from codepilot_s20 import agent_loop, basic_tools, runtime_state
from codepilot_s20.command_executor import (
    DockerCommandExecutor,
    LocalCommandExecutor,
    SandboxError,
)
from evals import run_eval
from evals.docker_sandbox import DockerGraderRunner, bind_mount, build_eval_image


class RecordingRunner:
    def __init__(self, behavior=None):
        self.calls = []
        self.behavior = behavior

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        if self.behavior:
            result = self.behavior(list(args), kwargs)
            if result is not None:
                return result
        return subprocess.CompletedProcess(args, 0, "ok\n", "")


def started_executor(tmp_path: Path, runner: RecordingRunner | None = None):
    runner = runner or RecordingRunner()
    executor = DockerCommandExecutor(
        workspace=tmp_path,
        image="eval:test",
        case_name="windows_path",
        runner=runner,
    )
    executor.start()
    return executor, runner


def test_local_command_executor_preserves_shell_execution(tmp_path):
    executor = LocalCommandExecutor()
    result = executor.execute(
        f'"{sys.executable}" -c "print(6 * 7)"',
        tmp_path,
        10,
    )

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "42"
    assert result["timed_out"] is False
    assert executor.execution_metadata()["command_execution_count"] == 1


def test_docker_agent_run_args_include_security_limits_and_one_mount(tmp_path):
    executor = DockerCommandExecutor(
        workspace=tmp_path,
        image="eval:test",
        case_name="security",
        memory="768m",
        cpus="0.5",
        pids_limit=64,
    )
    args = executor.docker_run_args()

    assert args[:3] == ["docker", "run", "--detach"]
    for expected in (
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "10001:10001",
        "--memory", "768m", "--memory-swap", "768m", "--cpus", "0.5",
        "--pids-limit", "64", "--ulimit", "nofile=256:256", "--tmpfs",
    ):
        assert expected in args
    mounts = [args[index + 1] for index, item in enumerate(args) if item == "--mount"]
    assert mounts == [bind_mount(tmp_path, "/workspace")]
    assert all("trusted_eval" not in mount and "grading_workspace" not in mount for mount in mounts)
    assert all(".env" not in arg for arg in args)


def test_windows_mount_is_one_argument_not_colon_delimited():
    value = bind_mount(Path(r"C:\Users\Example\agent_workspace"), "/workspace")

    assert value.startswith("type=bind,source=")
    assert ",target=/workspace" in value
    assert value.count("target=") == 1


def test_docker_bash_uses_exec_in_existing_container(tmp_path):
    executor, runner = started_executor(tmp_path)

    result = executor.execute("python -m pytest -q", tmp_path, 9)
    executor.stop()

    exec_args, exec_kwargs = runner.calls[1]
    assert exec_args[:3] == ["docker", "exec", "--workdir"]
    assert exec_args[-3:] == ["/bin/sh", "-lc", "python -m pytest -q"]
    assert exec_kwargs["timeout"] == 9
    assert result["exit_code"] == 0


def test_docker_unavailable_fails_without_local_fallback(tmp_path):
    def missing(_args, _kwargs):
        raise FileNotFoundError("docker")

    executor = DockerCommandExecutor(
        workspace=tmp_path,
        image="eval:test",
        case_name="missing",
        runner=RecordingRunner(missing),
    )

    with pytest.raises(SandboxError, match="failed to start"):
        executor.start()
    assert executor.command_execution_count == 0


def test_docker_start_failure_is_sandbox_error(tmp_path):
    def fail_run(args, _kwargs):
        if args[1] == "run":
            return subprocess.CompletedProcess(args, 125, "", "daemon unavailable")
        return subprocess.CompletedProcess(args, 0, "", "")

    executor = DockerCommandExecutor(
        workspace=tmp_path,
        image="eval:test",
        case_name="failure",
        runner=RecordingRunner(fail_run),
    )

    with pytest.raises(SandboxError, match="daemon unavailable"):
        executor.start()


def test_docker_exec_timeout_returns_structured_result_and_forces_cleanup(tmp_path):
    def timeout_exec(args, _kwargs):
        if args[1] == "exec":
            raise subprocess.TimeoutExpired(args, 2, output="partial", stderr="late")
        return subprocess.CompletedProcess(args, 0, "", "")

    executor, runner = started_executor(tmp_path, RecordingRunner(timeout_exec))
    result = executor.execute("sleep 99", tmp_path, 2)

    assert result == {
        "command": "sleep 99",
        "exit_code": None,
        "stdout": "partial",
        "stderr": "late",
        "timed_out": True,
        "duration_ms": result["duration_ms"],
    }
    assert result["duration_ms"] >= 0
    assert executor.execution_metadata()["container_timed_out"] is True
    assert any(call[0][:3] == ["docker", "rm", "-f"] for call in runner.calls)


def test_agent_sandbox_overall_timeout_forces_cleanup(tmp_path):
    runner = RecordingRunner()
    executor = DockerCommandExecutor(
        workspace=tmp_path,
        image="eval:test",
        case_name="overall-timeout",
        overall_timeout=0.01,
        runner=runner,
    )
    executor.start()
    threading.Event().wait(0.1)

    metadata = executor.execution_metadata()
    assert metadata["overall_timed_out"] is True
    assert metadata["container_timed_out"] is True
    assert metadata["container_cleanup_succeeded"] is True
    assert any(call[0][:3] == ["docker", "rm", "-f"] for call in runner.calls)


def test_agent_exception_and_keyboard_interrupt_always_stop_and_restore(tmp_path, monkeypatch):
    class LifecycleExecutor(LocalCommandExecutor):
        def __init__(self):
            super().__init__()
            self.started = 0
            self.stopped = 0

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1

    original = runtime_state.COMMAND_EXECUTOR

    for error in (RuntimeError("boom"), KeyboardInterrupt()):
        executor = LifecycleExecutor()

        def fail_loop(_messages, _context, error=error):
            raise error

        monkeypatch.setattr(agent_loop, "agent_loop", fail_loop)
        with pytest.raises(type(error)):
            agent_loop.run_agent_task(
                "fail", str(tmp_path), str(tmp_path / "trace.jsonl"),
                command_executor=executor,
            )
        assert executor.started == 1
        assert executor.stopped == 1
        assert runtime_state.COMMAND_EXECUTOR is original
        assert basic_tools.COMMAND_EXECUTOR is original


def test_grader_mounts_are_read_only_and_environment_is_allowlisted(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted_eval"
    grading = tmp_path / "grading_workspace"
    trusted.mkdir()
    grading.mkdir()
    files = {}
    for name in ("trace", "final", "stdout", "stderr"):
        files[name] = tmp_path / f"{name}.txt"
        files[name].write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    runner = DockerGraderRunner(image="eval:test", case_name="case")
    args = runner.docker_run_args(
        trusted_eval_root=trusted,
        grading_workspace=grading,
        trace_path=files["trace"],
        final_path=files["final"],
        stdout_path=files["stdout"],
        stderr_path=files["stderr"],
    )

    mounts = [args[index + 1] for index, item in enumerate(args) if item == "--mount"]
    assert len(mounts) == 6
    assert all(mount.endswith(",readonly") for mount in mounts)
    assert any("target=/trusted_eval,readonly" in mount for mount in mounts)
    assert any("target=/grading_workspace,readonly" in mount for mount in mounts)
    assert "must-not-leak" not in args
    assert "OPENAI_API_KEY" not in " ".join(args)


def test_summary_records_execution_metadata(tmp_path):
    config = run_eval.EvalExecutionConfig(
        backend="docker", docker_image="eval:test", docker_memory="512m",
        docker_cpus="0.5", docker_pids_limit=32,
    )
    summary = run_eval.build_summary(
        started=0, cases_dir=tmp_path, run_root=tmp_path,
        mode="scripted", results=[], execution_config=config,
    )

    assert summary["execution_backend"] == "docker"
    assert summary["docker_image"] == "eval:test"
    assert summary["resource_limits"]["memory"] == "512m"
    assert summary["resource_limits"]["pids_limit"] == 32


@pytest.mark.docker
def test_docker_eval_integration_smoke(tmp_path):
    try:
        available = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pytest.skip("Docker daemon is unavailable")
    if available.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    image = "codepilot-s20-eval:test"
    build_eval_image(project_root=run_eval.PROJECT_ROOT, image=image)
    config = run_eval.EvalExecutionConfig(backend="docker", docker_image=image)
    case = run_eval.PROJECT_ROOT / "evals" / "cases" / "read_file_basic"
    result = run_eval.run_case(case, tmp_path / "runs", scripted=True, execution_config=config)

    assert result["passed"] is True
    assert result["execution_backend"] == "docker"
    assert result["container_cleanup_succeeded"] is True
