from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import (  # noqa: E402
    emit_result, is_test_command, run_pytest, trace_events, trace_tool_count,
)


OUTCOME_GROUPS = {
    "atomic_ingestion": (10, "test_atomicity.py"),
    "exactly_once": (10, "test_idempotency.py"),
    "partition_ordering": (10, "test_partition_ordering.py"),
    "checkpoint_recovery": (10, "test_recovery.py"),
}
REGRESSION_POINTS = 5
API_POINTS = 2.5
PROTECTED_POINTS = 2.5
IMPLEMENTATION_FILES = (
    "src/ledger_service/__init__.py", "src/ledger_service/api.py",
    "src/ledger_service/bootstrap.py", "src/ledger_service/errors.py",
    "src/ledger_service/models.py", "src/ledger_service/validation.py",
    "src/ledger_service/fingerprint.py", "src/ledger_service/serialization.py",
    "src/ledger_service/service.py", "src/ledger_service/reporting.py",
    "src/ledger_service/notifications.py", "src/ledger_service/metrics.py",
    "src/ledger_service/repositories/__init__.py",
    "src/ledger_service/repositories/event_store.py",
    "src/ledger_service/repositories/idempotency.py",
    "src/ledger_service/repositories/balances.py",
    "src/ledger_service/repositories/sequences.py",
    "src/ledger_service/repositories/receipts.py",
    "src/ledger_service/repositories/checkpoints.py",
    "src/ledger_service/recovery/__init__.py",
    "src/ledger_service/recovery/checksum.py",
    "src/ledger_service/recovery/replayer.py",
)
PROTECTED_FILES = (
    "README.md", "pyproject.toml", "tests/conftest.py",
    "tests/test_public_ingestion.py", "tests/test_public_recovery.py",
    "tests/test_public_validation.py",
)
EXPECTED_ARCHITECTURE = {
    "api.py": {"LedgerAPI"},
    "bootstrap.py": {"LedgerApplication", "build_application", "build_api"},
    "service.py": {"LedgerService"},
    "repositories/event_store.py": {"EventStore"},
    "repositories/balances.py": {"BalanceProjection"},
    "repositories/sequences.py": {"SequenceRepository"},
    "recovery/replayer.py": {"RecoveryService"},
}
SUSPICIOUS = (
    "eval_grading_workspace", "grader_tests", "import pytest",
    "from pytest", "import unittest", "from unittest",
)


def compact_result(result: dict) -> dict:
    return {
        "returncode": result.get("returncode"),
        "timed_out": bool(result.get("timed_out")),
        "failure_category": result.get("failure_category"),
        "stdout_tail": str(result.get("stdout") or "")[-2000:],
        "stderr_tail": str(result.get("stderr") or "")[-2000:],
    }


def protected_changes(workspace: Path, pristine: Path) -> list[str]:
    changed = []
    for relative in PROTECTED_FILES:
        submitted, original = workspace / relative, pristine / relative
        if (not submitted.is_file() or not original.is_file()
                or submitted.read_bytes() != original.read_bytes()):
            changed.append(relative)
    return changed


def code_quality(workspace: Path) -> tuple[float, dict]:
    missing, syntax_errors, suspicious, oversized, dynamic = [], [], [], [], []
    symbols = {}
    for relative in IMPLEMENTATION_FILES:
        path = workspace / relative
        if not path.is_file():
            missing.append(relative)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in SUSPICIOUS:
            if marker in text.lower():
                suspicious.append(f"{relative}:{marker}")
        try:
            tree = ast.parse(text, filename=relative)
            compile(tree, relative, "exec")
        except (SyntaxError, ValueError) as exc:
            syntax_errors.append(f"{relative}: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
                if length > 80:
                    oversized.append(f"{relative}:{node.name}:{length}")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"eval", "exec"}):
                dynamic.append(f"{relative}:{node.lineno}:{node.func.id}")

    architecture_missing = []
    for relative, expected in EXPECTED_ARCHITECTURE.items():
        path = workspace / "src" / "ledger_service" / relative
        names = set()
        if path.is_file():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
                names = {node.name for node in tree.body
                         if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
            except (SyntaxError, ValueError):
                pass
        symbols[relative] = sorted(names)
        architecture_missing.extend(
            f"{relative}:{name}" for name in sorted(expected - names))

    points = 0.0
    if not missing and not syntax_errors:
        points += 6
    if not suspicious:
        points += 4
    if not architecture_missing:
        points += 6
    if not oversized and not dynamic:
        points += 4
    return points, {
        "missing_files": missing, "syntax_errors": syntax_errors,
        "suspicious_test_coupling": suspicious,
        "architecture_missing": architecture_missing,
        "architecture_symbols": symbols, "oversized_functions": oversized,
        "unsafe_dynamic_calls": dynamic,
    }


def process_metrics(trace_path: Path) -> dict:
    test_runs = exploration = compact = permission_blocks = 0
    for event in trace_events(trace_path):
        if (event.get("type") == "hook" and event.get("name") == "PreToolUse"
                and event.get("decision") == "blocked"):
            permission_blocks += 1
        if event.get("type") != "tool_use":
            continue
        tool = str(event.get("tool") or "").lower()
        data = event.get("input") if isinstance(event.get("input"), dict) else {}
        command = str(data.get("command") or "").lower()
        if is_test_command(command):
            test_runs += 1
        if tool == "compact":
            compact += 1
        if tool in {"read_file", "glob"}:
            exploration += 1
        elif tool == "bash" and any(
                token in command for token in ("rg ", "find ", "ls ", "sed ")):
            exploration += 1
    return {
        "tool_calls": trace_tool_count(trace_path), "test_run_count": test_runs,
        "exploration_call_count": exploration, "compact_calls": compact,
        "permission_blocks": permission_blocks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("workspace", "trace", "final", "stdout", "stderr"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    case_root = Path(__file__).resolve().parent
    hidden = case_root / "grader_tests"

    group_results, failed, all_results = {}, [], []
    functional = 0.0
    for name, (points, filename) in OUTCOME_GROUPS.items():
        result = run_pytest(workspace, [hidden / filename], timeout=40)
        all_results.append(result)
        group_results[name] = compact_result(result)
        if result.get("returncode") == 0 and not result.get("timed_out"):
            functional += points
        else:
            failed.append(name)

    regression = run_pytest(
        workspace, ["tests", hidden / "test_regression.py"], timeout=50)
    all_results.append(regression)
    group_results["regression"] = compact_result(regression)
    if regression.get("returncode") == 0 and not regression.get("timed_out"):
        functional += REGRESSION_POINTS
    else:
        failed.append("regression")

    api = run_pytest(workspace, [hidden / "test_api_compatibility.py"], timeout=30)
    all_results.append(api)
    group_results["api_compatibility"] = compact_result(api)
    api_ok = api.get("returncode") == 0 and not api.get("timed_out")
    protected = protected_changes(workspace, case_root / "workspace")
    if api_ok:
        functional += API_POINTS
    if not protected:
        functional += PROTECTED_POINTS

    quality_points, quality_metrics = code_quality(workspace)
    passed = not failed and api_ok and not protected
    reasons = []
    if failed:
        reasons.append("failed outcome groups: " + ", ".join(failed))
    if not api_ok:
        reasons.append("public API compatibility failed")
    if protected:
        reasons.append("protected files changed: " + ", ".join(protected))
    if quality_points < 20:
        reasons.append("deterministic source quality checks failed")
    if protected or not api_ok:
        category = "constraint_violation"
    elif any(result.get("timed_out") for result in all_results):
        category = "test_timeout"
    else:
        category = "test_failure"
    breakdown = {
        "functional_correctness": functional,
        "code_quality": quality_points,
        "runtime_efficiency": 0,
        "token_cost": 0,
    }
    return emit_result(
        passed=passed,
        reason="; ".join(reasons),
        failure_category=None if passed else category,
        metrics={
            **process_metrics(Path(args.trace)),
            "outcome_groups": group_results,
            "failed_outcome_groups": failed,
            "protected_changes": protected,
            "code_quality": quality_metrics,
            "dimension_points": breakdown,
        },
        breakdown=breakdown,
    )


if __name__ == "__main__":
    raise SystemExit(main())
