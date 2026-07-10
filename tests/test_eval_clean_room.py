from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals import run_eval


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTH_CASE = PROJECT_ROOT / "evals" / "cases" / "mini_auth_service_security_fix"
CATALOG_CASE = PROJECT_ROOT / "evals" / "cases" / "capability_catalog_generation"


def prepare_case(case_dir: Path, tmp_path: Path):
    agent = tmp_path / "agent"
    grading = tmp_path / "grading"
    metadata = run_eval.load_metadata(case_dir)
    before = run_eval.workspace_snapshot(case_dir / "workspace")
    run_eval.copy_case_workspace(case_dir, agent)
    return metadata, before, agent, grading


def build_and_apply(case_dir: Path, metadata: dict, before: dict, agent: Path, grading: Path):
    manifest = run_eval.build_change_manifest(
        before=before,
        after=run_eval.workspace_snapshot(agent),
        metadata=metadata,
    )
    run_eval.create_grading_workspace(
        case_dir=case_dir,
        agent_workspace=agent,
        grading_workspace=grading,
        manifest=manifest,
    )
    return manifest


def write_trace(path: Path):
    path.write_text(
        json.dumps({"type": "tool_use", "input": {"command": "python -m pytest -q"}}) + "\n",
        encoding="utf-8",
    )


def test_pytest_shim_tamper_is_violation_and_not_copied(tmp_path):
    metadata, before, agent, grading = prepare_case(AUTH_CASE, tmp_path)
    (agent / "pytest.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    manifest = build_and_apply(AUTH_CASE, metadata, before, agent, grading)

    assert "pytest.py" in manifest["unexpected_changes"]
    assert "pytest.py" in manifest["forbidden_changes"]
    assert not (grading / "pytest.py").exists()


def test_modified_public_test_is_constraint_violation(tmp_path):
    metadata, before, agent, grading = prepare_case(AUTH_CASE, tmp_path)
    test_path = agent / "tests" / "test_auth_service.py"
    test_path.write_text("def test_nothing(): assert True\n", encoding="utf-8")

    manifest = build_and_apply(AUTH_CASE, metadata, before, agent, grading)

    assert "tests/test_auth_service.py" in manifest["unexpected_changes"]
    assert "tests/test_auth_service.py" in manifest["forbidden_changes"]
    assert "def test_nothing" not in (grading / "tests" / "test_auth_service.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["conftest.py", "sitecustomize.py"])
def test_startup_hook_files_are_constraint_violations_and_not_copied(tmp_path, name):
    metadata, before, agent, grading = prepare_case(AUTH_CASE, tmp_path)
    (agent / name).write_text("raise SystemExit(0)\n", encoding="utf-8")

    manifest = build_and_apply(AUTH_CASE, metadata, before, agent, grading)

    assert name in manifest["unexpected_changes"]
    assert name in manifest["forbidden_changes"]
    assert not (grading / name).exists()


def test_correct_auth_fix_is_applied_and_grader_tests_pass(tmp_path):
    metadata, before, agent, grading = prepare_case(AUTH_CASE, tmp_path)
    (agent / "src" / "auth_service.py").write_text(
        '''USERS = {
    "alice": {"password": "wonderland", "role": "admin"},
    "bob": {"password": "builder", "role": "user"},
}


def authenticate(username, password):
    if username not in USERS:
        return False
    if password == "":
        return False
    return USERS[username]["password"] == password


def role_for(username):
    if username not in USERS:
        return None
    return USERS[username]["role"]
''',
        encoding="utf-8",
    )
    manifest = build_and_apply(AUTH_CASE, metadata, before, agent, grading)
    trace = tmp_path / "trace.jsonl"
    final = tmp_path / "final.md"
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    write_trace(trace)
    final.write_text("done", encoding="utf-8")
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")

    result, _proc = run_eval.run_grader(AUTH_CASE, grading, trace, final, stdout, stderr)

    assert manifest["submitted_changes"] == ["src/auth_service.py"]
    assert not manifest["unexpected_changes"]
    assert result["passed"] is True


def test_unexpected_new_file_is_not_copied_to_grading_workspace(tmp_path):
    metadata, before, agent, grading = prepare_case(AUTH_CASE, tmp_path)
    (agent / "notes.txt").write_text("not allowed\n", encoding="utf-8")

    manifest = build_and_apply(AUTH_CASE, metadata, before, agent, grading)

    assert "notes.txt" in manifest["unexpected_changes"]
    assert not (grading / "notes.txt").exists()


def test_allowed_glob_matches_processed_done_files(tmp_path):
    metadata, before, agent, grading = prepare_case(CATALOG_CASE, tmp_path)
    (agent / "catalog.csv").write_text("id,title,priority\n", encoding="utf-8")
    processed = agent / "processed"
    processed.mkdir()
    (processed / "A-100.done").write_text("processed", encoding="utf-8")

    manifest = build_and_apply(CATALOG_CASE, metadata, before, agent, grading)

    assert "catalog.csv" in manifest["submitted_changes"]
    assert "processed/A-100.done" in manifest["submitted_changes"]
    assert not manifest["unexpected_changes"]
    assert (grading / "processed" / "A-100.done").exists()


def test_unsafe_paths_and_symlinks_cannot_enter_grading_workspace(tmp_path):
    assert pytest.raises(ValueError, run_eval.safe_workspace_path, tmp_path, "../outside.py")
    assert pytest.raises(ValueError, run_eval.safe_workspace_path, tmp_path, str(tmp_path / "absolute.py"))

    metadata, before, agent, _grading = prepare_case(AUTH_CASE, tmp_path / "case")
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    link = agent / "src" / "auth_service.py"
    link.unlink()
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is not available in this environment")

    manifest = run_eval.build_change_manifest(
        before=before,
        after=run_eval.workspace_snapshot(agent),
        metadata=metadata,
    )

    assert "src/auth_service.py" in manifest["forbidden_changes"]


def test_original_case_workspace_snapshot_stays_unchanged(tmp_path):
    before = run_eval.workspace_snapshot(AUTH_CASE / "workspace")
    metadata, original, agent, grading = prepare_case(AUTH_CASE, tmp_path)
    (agent / "src" / "auth_service.py").write_text("# changed\n", encoding="utf-8")
    build_and_apply(AUTH_CASE, metadata, original, agent, grading)

    after = run_eval.workspace_snapshot(AUTH_CASE / "workspace")

    assert before == after


def test_change_manifest_records_added_modified_deleted_and_violations(tmp_path):
    metadata, before, agent, _grading = prepare_case(AUTH_CASE, tmp_path)
    (agent / "src" / "auth_service.py").write_text("# modified\n", encoding="utf-8")
    (agent / "extra.txt").write_text("added\n", encoding="utf-8")
    (agent / "tests" / "test_auth_service.py").unlink()

    manifest = run_eval.build_change_manifest(
        before=before,
        after=run_eval.workspace_snapshot(agent),
        metadata=metadata,
    )

    assert "extra.txt" in manifest["added"]
    assert "src/auth_service.py" in manifest["modified"]
    assert "tests/test_auth_service.py" in manifest["deleted"]
    assert "extra.txt" in manifest["unexpected_changes"]
    assert "tests/test_auth_service.py" in manifest["forbidden_changes"]


def test_runner_constraint_violation_overrides_passing_grader(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    workspace = case_dir / "workspace"
    workspace.mkdir(parents=True)
    (case_dir / "task.md").write_text("add bad file", encoding="utf-8")
    (case_dir / "metadata.yaml").write_text(
        "id: synthetic\nallowed_changes: []\nforbidden_paths: []\n",
        encoding="utf-8",
    )
    (case_dir / "grader.py").write_text("print('{}')\n", encoding="utf-8")

    def fake_agent(task, workdir, trace_path, **kwargs):
        Path(workdir, "bad.txt").write_text("bad", encoding="utf-8")
        Path(trace_path).write_text("", encoding="utf-8")
        return {"final_answer": "done"}

    def fake_grader(*args, **kwargs):
        return (
            {
                "passed": True,
                "score": 100,
                "breakdown": dict(run_eval.DEFAULT_BREAKDOWN_WEIGHTS),
                "metrics": {},
                "reason": "",
                "failure_category": None,
            },
            subprocess.CompletedProcess([], 0, "{}", ""),
        )

    monkeypatch.setattr(run_eval, "run_agent_task", fake_agent)
    monkeypatch.setattr(run_eval, "run_grader", fake_grader)

    result = run_eval.run_case(case_dir, tmp_path / "runs", scripted=True)

    assert result["passed"] is False
    assert result["failure_category"] == "constraint_violation"
    assert "bad.txt" in result["unexpected_changes"]
