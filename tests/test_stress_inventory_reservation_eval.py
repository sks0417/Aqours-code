from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import run_eval


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE = (
    PROJECT_ROOT
    / "evals"
    / "cases"
    / "stress_inventory_reservation_consistency"
)


def copy_workspace(target: Path) -> Path:
    run_eval.copy_case_workspace(CASE, target)
    return target


def write_complete_trace(path: Path) -> None:
    events = [
        {"type": "tool_use", "tool": "glob", "input": {"pattern": "src/**/*.py"}},
        {"type": "tool_use", "tool": "read_file", "input": {"path": "README.md"}},
        {"type": "tool_use", "tool": "read_file", "input": {"path": "src/inventory_service/service.py"}},
        {"type": "tool_use", "tool": "read_file", "input": {"path": "src/inventory_service/inventory_repository.py"}},
        {"type": "tool_use", "tool": "bash", "input": {"command": "python -m pytest -q"}},
        {"type": "tool_result", "tool": "bash", "content": "1 failed, 16 passed"},
        {"type": "tool_use", "tool": "edit_file", "input": {"path": "src/inventory_service/service.py"}},
        {"type": "tool_use", "tool": "bash", "input": {"command": "python -m pytest -q"}},
        {"type": "tool_result", "tool": "bash", "content": "17 passed"},
        {"type": "final_answer", "content": "Fixed and verified the reservation consistency issues."},
    ]
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def run_case_grader(workspace: Path, artifacts: Path) -> dict:
    artifacts.mkdir(parents=True, exist_ok=True)
    trace = artifacts / "trace.jsonl"
    final = artifacts / "final.md"
    stdout = artifacts / "stdout.txt"
    stderr = artifacts / "stderr.txt"
    write_complete_trace(trace)
    final.write_text("Implemented the documented fixes and ran tests.\n", encoding="utf-8")
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    result, _proc = run_eval.run_grader(
        CASE,
        workspace,
        trace,
        final,
        stdout,
        stderr,
    )
    return result


def replace_exact(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert old in source, f"reference patch anchor missing in {path}"
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def fix_atomic_reservation(workspace: Path) -> None:
    path = workspace / "src" / "inventory_service" / "inventory_repository.py"
    replace_exact(
        path,
        '''        lines = tuple(items)
        for line in lines:
            available = self.available(line.sku)
            if available < line.quantity:
                raise InsufficientInventory(
                    line.sku,
                    requested=line.quantity,
                    available=available,
                )
            self._available[line.sku] = available - line.quantity
''',
        '''        lines = tuple(items)
        for line in lines:
            available = self.available(line.sku)
            if available < line.quantity:
                raise InsufficientInventory(
                    line.sku,
                    requested=line.quantity,
                    available=available,
                )
        for line in lines:
            self._available[line.sku] -= line.quantity
''',
    )


def apply_controlled_solution(workspace: Path) -> None:
    fix_atomic_reservation(workspace)
    source_root = workspace / "src" / "inventory_service"
    replace_exact(
        source_root / "serialization.py",
        '{"sku": item.sku}\n            for item in request.items',
        '{"sku": item.sku, "quantity": item.quantity}\n            for item in request.items',
    )
    replace_exact(
        source_root / "service.py",
        '''        if existing is not None:
            return self.reservations.require(existing.reservation_id)
''',
        '''        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyConflict(idempotency_key)
            return self.reservations.require(existing.reservation_id)
''',
    )
    replace_exact(
        source_root / "service.py",
        "from .serialization import request_fingerprint\n",
        "from .serialization import request_fingerprint\nfrom .state import ReservationStatus\n",
    )
    replace_exact(
        source_root / "service.py",
        '''        reservation = self.reservations.require(reservation_id)
        reservation.cancel()
        self.inventory.release(reservation.items)
''',
        '''        reservation = self.reservations.require(reservation_id)
        if reservation.status is ReservationStatus.CANCELED:
            return reservation
        reservation.cancel()
        self.inventory.release(reservation.items)
''',
    )
    replace_exact(
        source_root / "state.py",
        "    ReservationStatus.CANCELED: {ReservationStatus.CONFIRMED},\n",
        "    ReservationStatus.CANCELED: set(),\n",
    )


@pytest.fixture(scope="module")
def original_result(tmp_path_factory):
    root = tmp_path_factory.mktemp("inventory-original")
    workspace = copy_workspace(root / "workspace")
    return run_case_grader(workspace, root / "artifacts")


@pytest.fixture(scope="module")
def correct_workspace_and_result(tmp_path_factory):
    root = tmp_path_factory.mktemp("inventory-correct")
    workspace = copy_workspace(root / "workspace")
    apply_controlled_solution(workspace)
    result = run_case_grader(workspace, root / "artifacts")
    return workspace, result, root


def test_case_metadata_structure_and_size_follow_existing_conventions():
    metadata = run_eval.load_metadata(CASE)
    discovered = {case.name for case in run_eval.discover_cases(CASE.parent)}
    workspace_files = [path for path in (CASE / "workspace").rglob("*") if path.is_file()]
    workspace_lines = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in workspace_files
    )

    assert CASE.name in discovered
    assert metadata["id"] == CASE.name
    assert metadata["suite"] == "stress"
    assert metadata["difficulty"] == 5
    assert metadata["allowed_changes"] == ["src/inventory_service/*.py"]
    assert set(metadata["forbidden_paths"]) == {
        "README.md",
        "pyproject.toml",
        "tests/**",
    }
    assert 12 <= len(workspace_files) <= 20
    assert 1000 <= workspace_lines <= 1800
    for required in ("task.md", "metadata.yaml", "grader.py", "grader_tests", "workspace"):
        assert (CASE / required).exists()


def test_faulty_workspace_exposes_core_defects_and_receives_partial_score(
    original_result,
):
    assert original_result["passed"] is False
    assert 0 < original_result["score"] < 100
    assert original_result["score"] == 32
    assert original_result["breakdown"] == {
        "functional_correctness": 12,
        "code_quality": 20,
        "runtime_efficiency": 0,
        "token_cost": 0,
    }
    groups = original_result["metrics"]["outcome_groups"]
    assert groups["atomic_reservation"]["returncode"] != 0
    assert groups["idempotent_retry"]["returncode"] == 0
    assert groups["idempotency_conflict"]["returncode"] != 0
    assert groups["cancellation_and_state"]["returncode"] != 0
    assert groups["regression"]["returncode"] != 0


def test_outcome_groups_award_independent_partial_credit(tmp_path, original_result):
    workspace = copy_workspace(tmp_path / "workspace")
    fix_atomic_reservation(workspace)

    result = run_case_grader(workspace, tmp_path / "artifacts")

    assert original_result["score"] < result["score"] < 100
    assert result["breakdown"]["functional_correctness"] == 24
    groups = result["metrics"]["outcome_groups"]
    assert groups["atomic_reservation"]["returncode"] == 0
    assert groups["idempotency_conflict"]["returncode"] != 0


def test_controlled_correct_implementation_passes_without_requiring_score_100(
    correct_workspace_and_result,
):
    _workspace, result, _root = correct_workspace_and_result

    assert result["passed"] is True
    assert result["score"] == 70
    assert result["breakdown"] == {
        "functional_correctness": 50,
        "code_quality": 20,
        "runtime_efficiency": 0,
        "token_cost": 0,
    }
    assert result["metrics"]["failed_outcome_groups"] == []
    assert all(
        group["returncode"] == 0
        for group in result["metrics"]["outcome_groups"].values()
    )


def test_protected_change_is_rejected_and_not_applied_to_clean_room(tmp_path):
    metadata = run_eval.load_metadata(CASE)
    before = run_eval.workspace_snapshot(CASE / "workspace")
    agent = copy_workspace(tmp_path / "agent")
    grading = tmp_path / "grading"
    (agent / "README.md").write_text("tampered contract\n", encoding="utf-8")

    manifest = run_eval.build_change_manifest(
        before=before,
        after=run_eval.workspace_snapshot(agent),
        metadata=metadata,
    )
    run_eval.create_grading_workspace(
        case_dir=CASE,
        agent_workspace=agent,
        grading_workspace=grading,
        manifest=manifest,
    )

    assert "README.md" in manifest["unexpected_changes"]
    assert "README.md" in manifest["forbidden_changes"]
    assert (grading / "README.md").read_bytes() == (CASE / "workspace" / "README.md").read_bytes()

    direct_result = run_case_grader(agent, tmp_path / "direct-artifacts")
    assert direct_result["passed"] is False
    assert direct_result["failure_category"] == "constraint_violation"
    assert direct_result["breakdown"]["functional_correctness"] == 9.5
    assert direct_result["metrics"]["protected_changes"] == ["README.md"]


def test_agent_workspace_copy_never_contains_trusted_grader_inputs(tmp_path):
    agent = copy_workspace(tmp_path / "agent")
    paths = {
        path.relative_to(agent).as_posix()
        for path in agent.rglob("*")
    }

    assert "grader.py" not in paths
    assert "grader_tests" not in paths
    assert not any(path.startswith("grader_tests/") for path in paths)
    assert "task.md" not in paths
    assert "metadata.yaml" not in paths
    assert "README.md" in paths
    assert "tests/test_public_reservations.py" in paths


def test_scoring_is_repeatable_and_uses_no_network_randomness(
    correct_workspace_and_result,
):
    workspace, first, root = correct_workspace_and_result
    second = run_case_grader(workspace, root / "repeat-artifacts")
    implementation_and_tests = [
        path
        for path in CASE.rglob("*.py")
        if path.name != "test_stress_inventory_reservation_eval.py"
    ]
    source_text = "\n".join(path.read_text(encoding="utf-8") for path in implementation_and_tests)

    assert second["score"] == first["score"] == 70
    assert second["breakdown"] == first["breakdown"]
    assert second["metrics"]["failed_outcome_groups"] == []
    assert "requests" not in source_text
    assert "urllib" not in source_text
    assert "socket" not in source_text
    assert "random" not in source_text
    assert "sleep(" not in source_text
