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
    "matching_semantics": (10, "test_matching_semantics.py"),
    "rollout_and_prerequisites": (10, "test_rollout_prerequisites.py"),
    "request_atomicity": (10, "test_request_atomicity.py"),
    "configuration_replacement": (10, "test_configuration_replacement.py"),
}
IMPLEMENTATION_FILES = (
    "src/rollout_engine/__init__.py", "src/rollout_engine/api.py",
    "src/rollout_engine/bootstrap.py", "src/rollout_engine/errors.py",
    "src/rollout_engine/models.py", "src/rollout_engine/validation.py",
    "src/rollout_engine/semver.py", "src/rollout_engine/predicates.py",
    "src/rollout_engine/bucketing.py", "src/rollout_engine/fingerprint.py",
    "src/rollout_engine/serialization.py", "src/rollout_engine/repositories.py",
    "src/rollout_engine/evaluator.py", "src/rollout_engine/service.py",
)
PROTECTED_FILES = (
    "README.md", "pyproject.toml", "tests/conftest.py",
    "tests/test_public_evaluation.py", "tests/test_public_configuration.py",
)
EXPECTED_ARCHITECTURE = {
    "api.py": {"RolloutAPI"},
    "bootstrap.py": {"RolloutApplication", "build_application", "build_api"},
    "repositories.py": {
        "ConfigurationRepository", "RequestRepository", "ExposureRepository"},
    "evaluator.py": {"FlagEvaluator"},
    "service.py": {"RolloutService"},
}
SUSPICIOUS = (
    "eval_grading_workspace", "grader_tests", "import pytest",
    "from pytest", "import unittest", "from unittest",
)


def _compact(result):
    return {
        "returncode": result.get("returncode"),
        "timed_out": bool(result.get("timed_out")),
        "failure_category": result.get("failure_category"),
        "stdout_tail": str(result.get("stdout") or "")[-2000:],
        "stderr_tail": str(result.get("stderr") or "")[-2000:],
    }


def _protected_changes(workspace: Path, pristine: Path):
    changed = []
    for relative in PROTECTED_FILES:
        left, right = workspace / relative, pristine / relative
        if (not left.is_file() or not right.is_file()
                or left.read_bytes() != right.read_bytes()):
            changed.append(relative)
    return changed


def _quality(workspace: Path):
    missing, syntax, suspicious, dynamic, architecture_missing = [], [], [], [], []
    trees = {}
    for relative in IMPLEMENTATION_FILES:
        path = workspace / relative
        if not path.is_file():
            missing.append(relative)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        suspicious.extend(
            f"{relative}:{marker}" for marker in SUSPICIOUS
            if marker in text.lower())
        try:
            tree = ast.parse(text, filename=relative)
            compile(tree, relative, "exec")
            trees[relative] = tree
        except (SyntaxError, ValueError) as exc:
            syntax.append(f"{relative}: {exc}")
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"eval", "exec"}):
                dynamic.append(f"{relative}:{node.lineno}:{node.func.id}")
    for relative, names in EXPECTED_ARCHITECTURE.items():
        tree = trees.get(f"src/rollout_engine/{relative}")
        actual = {
            node.name for node in (tree.body if tree else [])
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
        architecture_missing.extend(
            f"{relative}:{name}" for name in sorted(names - actual))
    points = 0.0
    if not missing and not syntax:
        points += 6
    if not suspicious:
        points += 4
    if not architecture_missing:
        points += 6
    if not dynamic:
        points += 4
    return points, {
        "missing_files": missing, "syntax_errors": syntax,
        "suspicious_test_coupling": suspicious,
        "architecture_missing": architecture_missing,
        "unsafe_dynamic_calls": dynamic,
    }


def _process_metrics(trace_path: Path):
    tests = exploration = compact = blocks = 0
    for event in trace_events(trace_path):
        if (event.get("type") == "hook" and event.get("name") == "PreToolUse"
                and event.get("decision") == "blocked"):
            blocks += 1
        if event.get("type") != "tool_use":
            continue
        tool = str(event.get("tool") or "").lower()
        data = event.get("input") if isinstance(event.get("input"), dict) else {}
        command = str(data.get("command") or "").lower()
        tests += int(is_test_command(command))
        compact += int(tool == "compact")
        exploration += int(
            tool in {"read_file", "glob"} or
            (tool == "bash" and any(x in command for x in ("rg ", "find ", "ls ", "sed "))))
    return {
        "tool_calls": trace_tool_count(trace_path), "test_run_count": tests,
        "exploration_call_count": exploration, "compact_calls": compact,
        "permission_blocks": blocks,
    }


def main():
    parser = argparse.ArgumentParser()
    for name in ("workspace", "trace", "final", "stdout", "stderr"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    case_root = Path(__file__).resolve().parent
    hidden = case_root / "grader_tests"
    functional, failed, results = 0.0, [], {}
    all_runs = []
    for name, (points, filename) in OUTCOME_GROUPS.items():
        run = run_pytest(workspace, [hidden / filename], timeout=40)
        all_runs.append(run)
        results[name] = _compact(run)
        if run.get("returncode") == 0 and not run.get("timed_out"):
            functional += points
        else:
            failed.append(name)
    regression = run_pytest(
        workspace, ["tests", hidden / "test_regression.py"], timeout=50)
    all_runs.append(regression)
    results["regression"] = _compact(regression)
    if regression.get("returncode") == 0 and not regression.get("timed_out"):
        functional += 5
    else:
        failed.append("regression")
    api = run_pytest(workspace, [hidden / "test_api_compatibility.py"], timeout=30)
    all_runs.append(api)
    results["api_compatibility"] = _compact(api)
    api_ok = api.get("returncode") == 0 and not api.get("timed_out")
    protected = _protected_changes(workspace, case_root / "workspace")
    if api_ok:
        functional += 2.5
    if not protected:
        functional += 2.5
    quality, quality_metrics = _quality(workspace)
    passed = not failed and api_ok and not protected and quality == 20
    reasons = []
    if failed:
        reasons.append("failed outcome groups: " + ", ".join(failed))
    if not api_ok:
        reasons.append("public API compatibility failed")
    if protected:
        reasons.append("protected files changed: " + ", ".join(protected))
    if quality < 20:
        reasons.append("deterministic source quality checks failed")
    if protected or not api_ok:
        category = "constraint_violation"
    elif any(run.get("timed_out") for run in all_runs):
        category = "test_timeout"
    else:
        category = "test_failure"
    breakdown = {
        "functional_correctness": functional, "code_quality": quality,
        "runtime_efficiency": 0, "token_cost": 0,
    }
    return emit_result(
        passed=passed, reason="; ".join(reasons),
        failure_category=None if passed else category,
        metrics={
            **_process_metrics(Path(args.trace)), "outcome_groups": results,
            "failed_outcome_groups": failed, "protected_changes": protected,
            "code_quality": quality_metrics, "dimension_points": breakdown,
        },
        breakdown=breakdown,
    )


if __name__ == "__main__":
    raise SystemExit(main())
