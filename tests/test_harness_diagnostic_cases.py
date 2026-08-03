from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from aqours_code import agent_loop as agent_loop_function
import aqours_code.agent_loop as agent_loop_module
from evals import run_eval
from evals.grader_common import run_pytest
from aqours_code.trace import TraceRun


ROOT = Path(__file__).resolve().parents[1]
CASES_ROOT = ROOT / "evals" / "cases"
CASE_NAMES = (
    "stress_context_contract_retention",
    "stress_context_verbose_test_diagnosis",
    "stress_team_parallel_module_migration",
    "stress_team_dependency_handoff",
)
REFERENCE_FILES = {
    CASE_NAMES[0]: (
        "src/notification_dispatcher/policy.py",
        "src/notification_dispatcher/service.py",
    ),
    CASE_NAMES[1]: ("src/inventory_import_pipeline/service.py",),
    CASE_NAMES[2]: (
        "src/billing_export/csv_export/encoder.py",
        "src/billing_export/json_export/encoder.py",
    ),
    CASE_NAMES[3]: (
        "src/document_index/storage.py",
        "src/document_index/query.py",
    ),
}


def _copy_reference(case_name: str, destination: Path) -> Path:
    case = CASES_ROOT / case_name
    workspace = destination / "workspace"
    shutil.copytree(case / "workspace", workspace)
    shutil.copytree(case / "reference_solution", workspace, dirs_exist_ok=True)
    return workspace


def _trace(case_name: str, path: Path) -> Path:
    if case_name == "stress_team_parallel_module_migration":
        events = [
            {"type": "shared_task_created", "task_id": "csv", "blocked_by": []},
            {"type": "shared_task_created", "task_id": "json", "blocked_by": []},
            {"type": "worktree_task_bound", "task_id": "csv", "worktree": "csv-wt"},
            {"type": "worktree_task_bound", "task_id": "json", "worktree": "json-wt"},
            {"type": "worktree_created", "task_id": "csv", "worktree": "csv-wt"},
            {"type": "worktree_created", "task_id": "json", "worktree": "json-wt"},
            {"type": "teammate_spawned", "teammate": "csv-worker"},
            {"type": "teammate_spawned", "teammate": "json-worker"},
            {"type": "shared_task_claimed", "task_id": "csv", "owner": "csv-worker"},
            {"type": "shared_task_claimed", "task_id": "json", "owner": "json-worker"},
            {"type": "message_bus_sent", "from_agent": "csv-worker", "to_agent": "lead"},
            {"type": "message_bus_sent", "from_agent": "json-worker", "to_agent": "lead"},
            {"type": "worktree_finalized", "worktree": "csv-wt"},
            {"type": "worktree_finalized", "worktree": "json-wt"},
            {"type": "shared_task_completed", "task_id": "csv"},
            {"type": "shared_task_completed", "task_id": "json"},
            {"type": "worktree_integrated", "worktree": "csv-wt"},
            {"type": "worktree_integrated", "worktree": "json-wt"},
        ]
    elif case_name == "stress_team_dependency_handoff":
        events = [
            {"type": "shared_task_created", "task_id": "storage", "blocked_by": []},
            {"type": "shared_task_created", "task_id": "query", "blocked_by": ["storage"]},
            {"type": "worktree_task_bound", "task_id": "storage", "worktree": "storage-wt"},
            {"type": "worktree_task_bound", "task_id": "query", "worktree": "query-wt"},
            {"type": "teammate_spawned", "teammate": "storage-worker"},
            {"type": "teammate_spawned", "teammate": "query-worker"},
            {"type": "shared_task_claimed", "task_id": "storage", "owner": "storage-worker"},
            {"type": "worktree_finalized", "worktree": "storage-wt"},
            {"type": "message_bus_sent", "from_agent": "storage-worker", "to_agent": "lead"},
            {"type": "shared_task_completed", "task_id": "storage"},
            {"type": "worktree_integrated", "worktree": "storage-wt"},
            {"type": "shared_task_claimed", "task_id": "query", "owner": "query-worker"},
            {"type": "worktree_finalized", "worktree": "query-wt"},
            {"type": "shared_task_completed", "task_id": "query"},
            {"type": "worktree_integrated", "worktree": "query-wt"},
        ]
    else:
        events = []
    path.write_text("".join(json.dumps(event) + "\n" for event in events),
                    encoding="utf-8")
    return path


def _grade(case_name: str, workspace: Path, trace: Path) -> dict:
    case = CASES_ROOT / case_name
    extras = {}
    for name in ("final", "stdout", "stderr"):
        target = trace.parent / f"{name}.txt"
        target.write_text("", encoding="utf-8")
        extras[name] = str(target)
    proc = subprocess.run(
        [sys.executable, str(case / "grader.py"),
         "--workspace", str(workspace), "--trace", str(trace),
         "--final", extras["final"], "--stdout", extras["stdout"],
         "--stderr", extras["stderr"]],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["passed"] is (proc.returncode == 0)
    return payload


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_new_case_metadata_discovery_and_clean_room(case_name, tmp_path):
    case = CASES_ROOT / case_name
    metadata = run_eval.load_metadata(case)
    assert metadata["id"] == case_name
    assert metadata["suite"] == "stress"
    assert metadata["difficulty"] == 4
    assert metadata["scripted_supported"] is False
    assert metadata["allowed_changes"] and metadata["forbidden_paths"]
    assert case in run_eval.discover_cases(CASES_ROOT)

    agent_workspace = tmp_path / "agent"
    run_eval.copy_case_workspace(case, agent_workspace)
    assert not (agent_workspace / "grader_tests").exists()
    assert not (agent_workspace / "reference_solution").exists()
    trusted = tmp_path / "trusted"
    trusted_case = run_eval.copy_trusted_case(case, trusted, case_name)
    assert (trusted / "stress_grader.py").is_file()
    assert (trusted_case / "grader_tests").is_dir()
    assert not (trusted_case / "reference_solution").exists()


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_initial_public_tests_execute_and_baseline_grader_fails(case_name, tmp_path):
    case = CASES_ROOT / case_name
    public = run_pytest(case / "workspace", ["tests"], timeout=40)
    assert public["timed_out"] is False
    assert "ERROR collecting" not in (public["stdout"] + public["stderr"])
    trace = _trace(case_name, tmp_path / "trace.jsonl")
    result = _grade(case_name, case / "workspace", trace)
    assert result["passed"] is False
    assert result["metrics"]["failed_outcome_groups"]


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_reference_implementation_passes_full_grader(case_name, tmp_path):
    workspace = _copy_reference(case_name, tmp_path)
    result = _grade(case_name, workspace, _trace(case_name, tmp_path / "trace.jsonl"))
    assert result["passed"] is True, result
    assert result["metrics"]["failed_outcome_groups"] == []


def _apply_mutant(case_name: str, workspace: Path, mutation: int):
    case = CASES_ROOT / case_name
    files = REFERENCE_FILES[case_name]
    if len(files) == 2:
        relative = files[mutation]
        shutil.copy2(case / "workspace" / relative, workspace / relative)
        return
    service = workspace / files[0]
    text = service.read_text(encoding="utf-8")
    if mutation == 0:
        text = text.replace(
            "cached = self.dedupe.lookup(idempotency_key, fingerprint)",
            "cached = self.dedupe.lookup(f'{idempotency_key}:{self.repository.count()}', fingerprint)",
        )
    else:
        text = text.replace("if duplicate is not None:", "if False:")
    service.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("case_name", CASE_NAMES)
@pytest.mark.parametrize("mutation", [0, 1])
def test_each_grader_rejects_two_wrong_implementations(case_name, mutation, tmp_path):
    workspace = _copy_reference(case_name, tmp_path)
    _apply_mutant(case_name, workspace, mutation)
    result = _grade(case_name, workspace, _trace(case_name, tmp_path / "trace.jsonl"))
    assert result["passed"] is False
    assert result["metrics"]["failed_outcome_groups"]


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_forbidden_test_tampering_and_symlinks_are_manifest_violations(
        case_name, tmp_path):
    case = CASES_ROOT / case_name
    workspace = tmp_path / "workspace"
    run_eval.copy_case_workspace(case, workspace)
    before = run_eval.workspace_snapshot(workspace)
    public_test = next((workspace / "tests").glob("test_*.py"))
    public_test.write_text("def test_tampered(): assert True\n", encoding="utf-8")
    (workspace / "conftest.py").write_text("# pytest tamper\n", encoding="utf-8")
    link = workspace / "src" / "linked.py"
    try:
        link.symlink_to(public_test)
    except OSError:
        pass
    manifest = run_eval.build_change_manifest(
        before=before, after=run_eval.workspace_snapshot(workspace),
        metadata=run_eval.load_metadata(case))
    assert public_test.relative_to(workspace).as_posix() in manifest["forbidden_changes"]
    assert "conftest.py" in manifest["forbidden_changes"]
    if link.is_symlink():
        assert "src/linked.py" in manifest["forbidden_changes"]


class _OneTurnMessages:
    def create(self, **_kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="done")],
            stop_reason="end_turn", usage=None,
        )


class _OneTurnClient:
    def __init__(self):
        self.messages = _OneTurnMessages()


@pytest.mark.parametrize("limit", [15_999, 2_000_001])
def test_eval_context_limit_rejects_out_of_bounds_and_restores_state(limit, tmp_path):
    original = agent_loop_module.CONTEXT_LIMIT
    with pytest.raises(ValueError, match="context_limit_chars"):
        agent_loop_module.run_agent_task(
            "say done", str(tmp_path), model_client=_OneTurnClient(),
            model_provider="test", model="test", context_limit_chars=limit)
    assert agent_loop_module.CONTEXT_LIMIT == original


@pytest.mark.parametrize("ratio", [0.249, 0.951])
def test_eval_compact_ratio_rejects_out_of_bounds_and_restores_state(ratio, tmp_path):
    original = agent_loop_module.COMPACT_TRIGGER_RATIO
    with pytest.raises(ValueError, match="compact_trigger_ratio"):
        agent_loop_module.run_agent_task(
            "say done", str(tmp_path), model_client=_OneTurnClient(),
            model_provider="test", model="test", compact_trigger_ratio=ratio)
    assert agent_loop_module.COMPACT_TRIGGER_RATIO == original


def test_eval_context_override_is_traced_and_restored(tmp_path):
    original_limit = agent_loop_module.CONTEXT_LIMIT
    original_ratio = agent_loop_module.COMPACT_TRIGGER_RATIO
    trace = tmp_path / "trace.jsonl"
    agent_loop_module.run_agent_task(
        "say done", str(tmp_path), str(trace), model_client=_OneTurnClient(),
        model_provider="test", model="test", context_limit_chars=50_000,
        compact_trigger_ratio=0.7)
    event = next(event for event in run_eval.read_trace_events(trace)
                 if event.get("type") == "context_configuration")
    assert event["context_limit_chars"] == 50_000
    assert event["compact_trigger_ratio"] == 0.7
    assert event["eval_override"] is True
    assert agent_loop_module.CONTEXT_LIMIT == original_limit
    assert agent_loop_module.COMPACT_TRIGGER_RATIO == original_ratio


def test_trace_run_serializes_concurrent_teammate_events(tmp_path):
    run = TraceRun(tmp_path, "test", "test")
    threads = [threading.Thread(
        target=lambda worker=worker: [
            run.event("teammate_tool_use", teammate=worker, sequence=index)
            for index in range(40)
        ]) for worker in ("one", "two", "three")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    run.finish("done")
    events = run.trace_path.read_text(encoding="utf-8").splitlines()
    assert len(events) == 120
    assert all(json.loads(line)["type"] == "teammate_tool_use" for line in events)
