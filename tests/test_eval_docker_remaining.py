from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from codepilot_s20.command_executor import DockerCommandExecutor, SandboxError
from codepilot_s20.docker_utils import prepare_disposable_tree
from evals import run_eval
from evals.docker_sandbox import DockerGraderRunner


def agent_metadata(**updates):
    value = {
        "execution_backend": "docker",
        "docker_image": "eval:test",
        "container_started": True,
        "container_exit_code": 0,
        "container_timed_out": False,
        "container_cleanup_succeeded": True,
        "command_execution_count": 0,
        "resource_limits": {},
        "sandbox_error": "",
        "command_timed_out": False,
        "overall_timed_out": False,
        "model_broker_stopped": True,
        "model_broker_ipc_cleaned": True,
        "agent_state_cleaned": True,
    }
    value.update(updates)
    return value


def passing_grader_result():
    return {
        "passed": True,
        "score": 100,
        "breakdown": dict(run_eval.DEFAULT_BREAKDOWN_WEIGHTS),
        "metrics": {},
        "reason": "",
        "failure_category": None,
    }


def test_grader_receives_only_remaining_case_budget(tmp_path, monkeypatch):
    observed = {}

    def fake_agent(**kwargs):
        time.sleep(0.3)
        return {"final_answer": "done"}, "", agent_metadata()

    def fake_grader(**kwargs):
        observed["remaining"] = run_eval.remaining_timeout(kwargs["case_deadline"])
        return (
            passing_grader_result(),
            subprocess.CompletedProcess([], 0, "", ""),
            {"container_started": True, "container_exit_code": 0,
             "cleanup_succeeded": True, "timed_out": False},
        )

    monkeypatch.setattr(run_eval, "_run_docker_agent_phase", fake_agent)
    monkeypatch.setattr(run_eval, "run_docker_grader", fake_grader)
    monkeypatch.setattr(
        run_eval,
        "prepare_docker_disposable_paths",
        lambda *_args, **_kwargs: None,
    )
    result = run_eval.run_case(
        run_eval.PROJECT_ROOT / "evals" / "cases" / "read_file_basic",
        tmp_path / "runs", scripted=True,
        execution_config=run_eval.EvalExecutionConfig(
            backend="docker", docker_image="eval:test", docker_timeout=0.8),
    )

    assert result["passed"] is True
    assert 0 < observed["remaining"] < 0.6
    assert result["all_container_cleanup_succeeded"] is True


def test_stuck_grader_and_cleanup_end_within_deadline_plus_shared_grace(
    tmp_path, monkeypatch,
):
    real_runner = DockerGraderRunner
    observed_timeouts = []

    def fake_agent(**kwargs):
        return {"final_answer": "done"}, "", agent_metadata()

    def hanging_runner(args, **kwargs):
        timeout = kwargs["timeout"]
        observed_timeouts.append((args[1], timeout))
        time.sleep(timeout)
        raise subprocess.TimeoutExpired(args, timeout)

    def runner_factory(**kwargs):
        return real_runner(**kwargs, runner=hanging_runner)

    monkeypatch.setattr(run_eval, "_run_docker_agent_phase", fake_agent)
    monkeypatch.setattr(run_eval, "DockerGraderRunner", runner_factory)
    monkeypatch.setattr(run_eval, "CLEANUP_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(
        run_eval,
        "prepare_docker_disposable_paths",
        lambda *_args, **_kwargs: None,
    )
    started = time.monotonic()
    result = run_eval.run_case(
        run_eval.PROJECT_ROOT / "evals" / "cases" / "read_file_basic",
        tmp_path / "runs", scripted=True,
        execution_config=run_eval.EvalExecutionConfig(
            backend="docker", docker_image="eval:test", docker_timeout=0.25),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.8
    assert result["failure_category"] == "test_timeout"
    assert result["grader_container_cleanup_succeeded"] is False
    assert result["all_container_cleanup_succeeded"] is False
    assert [kind for kind, _timeout in observed_timeouts] == ["run", "rm"]
    assert sum(timeout for _kind, timeout in observed_timeouts) <= 0.5


def test_agent_cleanup_commands_share_one_absolute_deadline(tmp_path):
    calls = []

    def runner(args, **kwargs):
        timeout = kwargs["timeout"]
        calls.append((args[1], timeout))
        time.sleep(timeout)
        raise subprocess.TimeoutExpired(args, timeout)

    deadline = time.monotonic() + 0.12
    executor = DockerCommandExecutor(
        workspace=tmp_path, image="eval:test", case_name="cleanup-budget",
        verify_workspace_write=False, operation_deadline=deadline, runner=runner,
    )
    started = time.monotonic()
    executor.stop(deadline=deadline)
    elapsed = time.monotonic() - started

    assert elapsed < 0.3
    assert calls[0][0] == "inspect"
    assert all(0 < timeout <= 0.12 for _kind, timeout in calls)
    assert len(calls) <= 2
    assert executor.execution_metadata()["container_cleanup_succeeded"] is False


def test_root_host_permission_preparation_stays_inside_disposable_copy(tmp_path):
    case_output = tmp_path / "case-output"
    workspace = case_output / "agent_workspace"
    workspace.mkdir(parents=True)
    inside = workspace / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    workspace.chmod(0o700)
    inside.chmod(0o600)
    changed = []

    def fake_chown(path, uid, gid, *, follow_symlinks):
        changed.append((Path(path), uid, gid, follow_symlinks))

    assert prepare_disposable_tree(
        workspace, allowed_root=case_output, platform="posix",
        getuid=lambda: 0, chown=fake_chown,
    ) is True

    changed_paths = {path for path, _uid, _gid, _follow in changed}
    assert inside in changed_paths
    assert workspace.resolve() in changed_paths
    assert outside not in changed_paths
    assert all(uid == 10001 and gid == 10001 and follow is False
               for _path, uid, gid, follow in changed)
    with pytest.raises(ValueError, match="escapes case output"):
        prepare_disposable_tree(
            outside, allowed_root=case_output, platform="posix",
            getuid=lambda: 0, chown=fake_chown,
        )


def test_root_host_permission_preparation_does_not_follow_symlink(tmp_path):
    case_output = tmp_path / "case-output"
    workspace = case_output / "agent_workspace"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = workspace / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available in this environment")
    changed = []

    prepare_disposable_tree(
        workspace, allowed_root=case_output, platform="posix",
        getuid=lambda: 0,
        chown=lambda path, *_args, **_kwargs: changed.append(Path(path)),
    )

    assert link not in changed
    assert outside not in changed


def test_chown_failure_is_fail_closed_sandbox_error_before_agent_or_grader(
    tmp_path, monkeypatch,
):
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")

    def failing_prepare(path, *, allowed_root):
        def failing_chown(_path, _uid, _gid, *, follow_symlinks):
            assert follow_symlinks is False
            raise OSError("root-squash operation not permitted")

        return prepare_disposable_tree(
            path, allowed_root=allowed_root, platform="posix",
            getuid=lambda: 0, chown=failing_chown,
        )

    monkeypatch.setattr(run_eval, "prepare_disposable_tree", failing_prepare)
    monkeypatch.setattr(
        run_eval, "_run_docker_agent_phase",
        lambda **_kwargs: pytest.fail("Agent must not start after chown failure"),
    )
    monkeypatch.setattr(
        run_eval, "run_docker_grader",
        lambda **_kwargs: pytest.fail("Grader must not start after chown failure"),
    )
    case = run_eval.PROJECT_ROOT / "evals" / "cases" / "read_file_basic"
    run_root = tmp_path / "runs"

    with pytest.raises(SandboxError) as caught:
        run_eval.run_case(
            case, run_root, scripted=True,
            execution_config=run_eval.EvalExecutionConfig(backend="docker"),
        )

    agent_workspace = run_root / case.name / "agent_workspace"
    message = str(caught.value)
    assert "unable to prepare disposable workspace for the non-root Docker user" in message
    assert str(agent_workspace) in message
    assert "root-squash operation not permitted" in message
    result = run_eval.case_exception_result(
        case, run_root, caught.value,
        run_eval.EvalExecutionConfig(backend="docker"),
    )
    assert result["failure_category"] == "sandbox_error"
    assert outside.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize(
    ("agent_cleanup", "grader_cleanup", "expected"),
    [(True, False, False), (False, True, False), (True, True, True)],
)
def test_summary_aggregates_agent_and_grader_cleanup(
    tmp_path, agent_cleanup, grader_cleanup, expected,
):
    result = {
        "passed": True,
        "score": 100,
        "metrics": {},
        "metadata": {"suite": "test", "difficulty": 1},
        "failure_category": None,
        "agent_container_cleanup_succeeded": agent_cleanup,
        "grader_container_cleanup_succeeded": grader_cleanup,
        "all_container_cleanup_succeeded": agent_cleanup and grader_cleanup,
        "agent_container_started": True,
        "grader_container_started": True,
        "agent_container_exit_code": 0,
        "grader_container_exit_code": 0,
        "grader_execution": {"timed_out": False},
    }
    summary = run_eval.build_summary(
        started=time.time(), cases_dir=tmp_path, run_root=tmp_path,
        mode="scripted", results=[result],
        execution_config=run_eval.EvalExecutionConfig(backend="docker"),
    )

    assert summary["container_cleanup_succeeded"] is expected
    assert summary["all_container_cleanup_succeeded"] is expected
    assert summary["agent_container_cleanup_succeeded"] is agent_cleanup
    assert summary["grader_container_cleanup_succeeded"] is grader_cleanup


def test_grader_not_started_is_explicit_and_not_cleanup_success(tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    result = run_eval.case_exception_result(
        case, tmp_path / "runs", RuntimeError("early"),
        run_eval.EvalExecutionConfig(backend="docker"),
    )

    assert result["grader_execution"]["status"] == "not_started"
    assert result["grader_container_started"] is None
    assert result["grader_container_cleanup_succeeded"] is None
    assert result["all_container_cleanup_succeeded"] is False


def test_default_scripted_mode_selects_only_supported_smoke_cases(
    tmp_path, monkeypatch,
):
    called = []

    def fake_run_case(case, _run_root, _scripted, _config):
        called.append(case.name)
        return {
            "passed": True, "score": 100, "metrics": {},
            "metadata": run_eval.load_metadata(case), "failure_category": None,
            "reason": "", "command_execution_count": 0,
            "all_container_cleanup_succeeded": True,
        }

    monkeypatch.setattr(run_eval, "run_case", fake_run_case)
    monkeypatch.setattr(sys, "argv", [
        "run_eval", "--scripted", "--execution", "local",
        "--results-dir", str(tmp_path / "results"),
    ])
    assert run_eval.main() == 0
    assert called == [
        "edit_file_basic", "permission_denied_basic", "read_file_basic",
        "run_tests_basic", "trace_record_basic",
    ]


def test_explicit_unsupported_scripted_case_returns_clear_error(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(sys, "argv", [
        "run_eval", "--scripted", "--case", "capability_json_update",
        "--results-dir", str(tmp_path / "results"),
    ])
    with pytest.raises(SystemExit) as caught:
        run_eval.main()

    assert caught.value.code == 2
    assert "scripted mode is not supported for: capability_json_update" in capsys.readouterr().err
