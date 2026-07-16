from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import (  # noqa: E402
    emit_result,
    is_test_command,
    run_pytest,
    trace_events,
    trace_tool_count,
)


OUTCOME_GROUPS = {
    "atomic_reservation": (12, "test_atomicity.py"),
    "idempotent_retry": (7, "test_idempotent_retry.py"),
    "idempotency_conflict": (6, "test_idempotency_conflict.py"),
    "cancellation_and_state": (10, "test_cancellation_state.py"),
}
REGRESSION_POINTS = 5
API_COMPATIBILITY_POINTS = 10
PROTECTED_INPUT_POINTS = 5

IMPLEMENTATION_FILES = (
    "src/inventory_service/__init__.py",
    "src/inventory_service/api.py",
    "src/inventory_service/bootstrap.py",
    "src/inventory_service/errors.py",
    "src/inventory_service/idempotency_repository.py",
    "src/inventory_service/inventory_repository.py",
    "src/inventory_service/models.py",
    "src/inventory_service/reservation_repository.py",
    "src/inventory_service/serialization.py",
    "src/inventory_service/service.py",
    "src/inventory_service/state.py",
    "src/inventory_service/validation.py",
)
PROTECTED_WORKSPACE_FILES = (
    "README.md",
    "pyproject.toml",
    "tests/conftest.py",
    "tests/test_public_reservations.py",
    "tests/test_public_validation.py",
)
EXPECTED_ARCHITECTURE = {
    "api.py": {"ReservationAPI"},
    "bootstrap.py": {"InventoryApplication"},
    "idempotency_repository.py": {"IdempotencyRepository"},
    "inventory_repository.py": {"InventoryRepository"},
    "reservation_repository.py": {"ReservationRepository"},
    "service.py": {"ReservationService", "SequentialReservationId"},
    "state.py": {"ReservationStatus"},
}
SUSPICIOUS_TEST_MARKERS = (
    "eval_grading_workspace",
    "grader_tests",
    "import pytest",
    "from pytest",
    "import unittest",
    "from unittest",
)


def compact_test_result(result: dict) -> dict:
    return {
        "returncode": result.get("returncode"),
        "timed_out": bool(result.get("timed_out")),
        "failure_category": result.get("failure_category"),
        "stdout_tail": str(result.get("stdout") or "")[-2000:],
        "stderr_tail": str(result.get("stderr") or "")[-2000:],
    }


def run_group(workspace: Path, grader_tests: Path, filename: str) -> dict:
    return run_pytest(workspace, [grader_tests / filename], timeout=30)


def protected_inputs_unchanged(workspace: Path, pristine: Path) -> list[str]:
    changed = []
    for relative in PROTECTED_WORKSPACE_FILES:
        submitted = workspace / relative
        original = pristine / relative
        if not submitted.is_file() or not original.is_file():
            changed.append(relative)
            continue
        if submitted.read_bytes() != original.read_bytes():
            changed.append(relative)
    return changed


def assess_code_quality(workspace: Path) -> tuple[float, dict]:
    source_root = workspace / "src" / "inventory_service"
    missing = []
    syntax_errors = []
    suspicious = []
    discovered_architecture: dict[str, list[str]] = {}

    for relative in IMPLEMENTATION_FILES:
        path = workspace / relative
        if not path.is_file():
            missing.append(relative)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        for marker in SUSPICIOUS_TEST_MARKERS:
            if marker in lowered:
                suspicious.append(f"{relative}:{marker}")
        try:
            tree = ast.parse(text, filename=relative)
            compile(tree, relative, "exec")
        except (SyntaxError, ValueError) as exc:
            syntax_errors.append(f"{relative}: {exc}")

    architecture_missing = []
    for filename, expected_names in EXPECTED_ARCHITECTURE.items():
        path = source_root / filename
        names: set[str] = set()
        if path.is_file():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=filename)
            except (SyntaxError, ValueError):
                tree = None
            if tree is not None:
                names = {
                    node.name
                    for node in tree.body
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                }
        discovered_architecture[filename] = sorted(names)
        for name in sorted(expected_names - names):
            architecture_missing.append(f"{filename}:{name}")

    points = 0.0
    if not missing and not syntax_errors:
        points += 6
    if not suspicious:
        points += 4
    if not architecture_missing:
        points += 5
    return points, {
        "missing_files": missing,
        "syntax_errors": syntax_errors,
        "suspicious_test_coupling": suspicious,
        "architecture_missing": architecture_missing,
        "architecture_symbols": discovered_architecture,
    }


def trace_process_metrics(trace_path: Path) -> dict:
    events = trace_events(trace_path)
    test_runs = 0
    exploration_calls = 0
    compact_calls = 0
    permission_blocks = 0

    for event in events:
        if (
            event.get("type") == "hook"
            and event.get("name") == "PreToolUse"
            and event.get("decision") == "blocked"
        ):
            permission_blocks += 1
        if event.get("type") != "tool_use":
            continue
        tool = str(event.get("tool") or event.get("name") or "").lower()
        tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
        command = str(tool_input.get("command") or tool_input.get("cmd") or "").lower()
        if is_test_command(command):
            test_runs += 1
        if tool == "compact":
            compact_calls += 1
        if tool in {"read_file", "glob"}:
            exploration_calls += 1
        elif tool in {"bash", "shell", "powershell", "cmd"} and any(
            token in command
            for token in ("rg ", "rg --files", "find ", "ls ", "get-childitem", "sed ", "head ", "tail ")
        ):
            exploration_calls += 1

    return {
        "tool_calls": trace_tool_count(trace_path),
        "test_run_count": test_runs,
        "exploration_call_count": exploration_calls,
        "permission_blocks": permission_blocks,
        "compact_calls": compact_calls,
    }


def process_score(metrics: dict) -> float:
    points = 0.0
    if metrics["test_run_count"] >= 1:
        points += 10
    if metrics["test_run_count"] >= 2:
        points += 5
    if metrics["exploration_call_count"] >= 4:
        points += 5
    return points


def efficiency_score(metrics: dict, test_results: list[dict]) -> float:
    points = 0.0
    if 1 <= metrics["tool_calls"] <= 80:
        points += 4
    elif 1 <= metrics["tool_calls"] <= 120:
        points += 2
    if metrics["permission_blocks"] == 0:
        points += 2
    if metrics["compact_calls"] <= 1:
        points += 1
    if not any(result.get("timed_out") for result in test_results):
        points += 3
    return points


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--final", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    case_root = Path(__file__).resolve().parent
    grader_tests = case_root / "grader_tests"
    pristine_workspace = case_root / "workspace"

    group_results: dict[str, dict] = {}
    outcome_points = 0.0
    failed_groups = []
    all_test_results = []

    for group_name, (points, filename) in OUTCOME_GROUPS.items():
        result = run_group(workspace, grader_tests, filename)
        all_test_results.append(result)
        group_results[group_name] = compact_test_result(result)
        if result.get("returncode") == 0 and not result.get("timed_out"):
            outcome_points += points
        else:
            failed_groups.append(group_name)

    regression = run_pytest(
        workspace,
        ["tests", grader_tests / "test_regression.py"],
        timeout=35,
    )
    all_test_results.append(regression)
    group_results["regression"] = compact_test_result(regression)
    if regression.get("returncode") == 0 and not regression.get("timed_out"):
        outcome_points += REGRESSION_POINTS
    else:
        failed_groups.append("regression")

    api_compatibility = run_group(
        workspace,
        grader_tests,
        "test_api_compatibility.py",
    )
    all_test_results.append(api_compatibility)
    group_results["api_compatibility"] = compact_test_result(api_compatibility)

    protected_changes = protected_inputs_unchanged(
        workspace,
        pristine_workspace,
    )
    constraints_points = 0.0
    if api_compatibility.get("returncode") == 0 and not api_compatibility.get("timed_out"):
        constraints_points += API_COMPATIBILITY_POINTS
    if not protected_changes:
        constraints_points += PROTECTED_INPUT_POINTS

    trace_metrics = trace_process_metrics(Path(args.trace))
    process_points = process_score(trace_metrics)
    code_quality_points, code_quality_metrics = assess_code_quality(workspace)
    efficiency_points = efficiency_score(trace_metrics, all_test_results)

    breakdown = {
        "outcome_correctness": outcome_points,
        "constraints": constraints_points,
        "process_quality": process_points,
        "code_quality": code_quality_points,
        "efficiency": efficiency_points,
    }
    score = sum(breakdown.values())
    passed = score == 100 and not failed_groups and not protected_changes

    reasons = []
    if failed_groups:
        reasons.append("failed test groups: " + ", ".join(failed_groups))
    if api_compatibility.get("returncode") != 0:
        reasons.append("public API or exception compatibility failed")
    if protected_changes:
        reasons.append("protected files changed: " + ", ".join(protected_changes))
    if process_points < 20:
        reasons.append(
            "process evidence incomplete "
            f"(tests={trace_metrics['test_run_count']}, "
            f"exploration={trace_metrics['exploration_call_count']})"
        )
    if code_quality_points < 15:
        reasons.append("deterministic source quality checks failed")
    if efficiency_points < 10:
        reasons.append("efficiency checks were not fully satisfied")

    if protected_changes or api_compatibility.get("returncode") != 0:
        category = "constraint_violation"
    elif any(result.get("timed_out") for result in all_test_results):
        category = "test_timeout"
    else:
        category = "test_failure"

    metrics = {
        **trace_metrics,
        "outcome_groups": group_results,
        "failed_outcome_groups": failed_groups,
        "protected_changes": protected_changes,
        "code_quality": code_quality_metrics,
        "dimension_points": breakdown,
    }
    return emit_result(
        passed=passed,
        reason="; ".join(reasons),
        failure_category=None if passed else category,
        metrics=metrics,
        breakdown=breakdown,
    )


if __name__ == "__main__":
    raise SystemExit(main())
