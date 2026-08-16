from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path

from grader_common import (
    emit_result, is_test_command, run_pytest, trace_events, trace_tool_count,
)


SUSPICIOUS = (
    "eval_grading_workspace", "grader_tests", "import pytest",
    "from pytest", "import unittest", "from unittest",
)


def _compact(result: dict) -> dict:
    return {
        "returncode": result.get("returncode"),
        "timed_out": bool(result.get("timed_out")),
        "failure_category": result.get("failure_category"),
        "stdout_tail": str(result.get("stdout") or "")[-2000:],
        "stderr_tail": str(result.get("stderr") or "")[-2000:],
    }


def _protected_changes(workspace: Path, pristine: Path, protected) -> list[str]:
    changed = []
    for relative in protected:
        submitted, original = workspace / relative, pristine / relative
        if (not submitted.is_file() or not original.is_file()
                or submitted.read_bytes() != original.read_bytes()):
            changed.append(relative)
    return changed


def _quality(workspace: Path, package: str, files, architecture):
    missing, syntax, suspicious, dynamic, architecture_missing = [], [], [], [], []
    trees = {}
    for relative in files:
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
    for relative, names in architecture.items():
        tree = trees.get(f"src/{package}/{relative}")
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
        "missing_files": missing,
        "syntax_errors": syntax,
        "suspicious_test_coupling": suspicious,
        "architecture_missing": architecture_missing,
        "unsafe_dynamic_calls": dynamic,
    }


def _process_metrics(trace_path: Path) -> dict:
    test_runs = exploration = compact = permission_blocks = 0
    events = trace_events(trace_path)
    test_commands = []
    teammate_model_calls = Counter()
    teammate_tool_calls = Counter()
    for event in events:
        if event.get("type") == "llm_request" and event.get("purpose") == "teammate":
            teammate_model_calls[str(event.get("agent_role") or "unknown")] += 1
        if event.get("type") == "teammate_tool_use":
            teammate_tool_calls[str(event.get("teammate") or "unknown")] += 1
        if (event.get("type") == "hook" and event.get("name") == "PreToolUse"
                and event.get("decision") == "blocked"):
            permission_blocks += 1
        if event.get("type") != "tool_use":
            continue
        tool = str(event.get("tool") or "").lower()
        data = event.get("input") if isinstance(event.get("input"), dict) else {}
        command = str(data.get("command") or "").lower()
        test_runs += int(is_test_command(command))
        if is_test_command(command):
            test_commands.append(command.strip())
        compact += int(tool == "compact")
        exploration += int(
            tool in {"read_file", "glob"}
            or (tool == "bash" and any(
                token in command for token in ("rg ", "find ", "ls ", "sed "))))
    test_counts = Counter(test_commands)
    sent = Counter((e.get("from_agent"), e.get("to_agent"), e.get("message_type"))
                   for e in events if e.get("type") == "message_bus_sent")
    received = Counter((e.get("from_agent"), e.get("agent"), e.get("message_type"))
                       for e in events if e.get("type") == "message_bus_received")
    message_signatures = Counter((
        e.get("from_agent"), e.get("to_agent"), e.get("message_type"),
        e.get("content_preview")) for e in events
        if e.get("type") == "message_bus_sent")
    message_latencies = [
        max(0.0, float(e.get("ts")) - float(e.get("sent_ts")))
        for e in events if e.get("type") == "message_bus_received"
        and isinstance(e.get("ts"), (int, float))
        and isinstance(e.get("sent_ts"), (int, float))
    ]
    return {
        "tool_calls": trace_tool_count(trace_path),
        "test_run_count": test_runs,
        "exploration_call_count": exploration,
        "compact_calls": compact,
        "permission_blocks": permission_blocks,
        "automatic_compactions": sum(1 for event in events
            if event.get("type") == "compact" and event.get("kind") == "automatic"
            and event.get("success") is True),
        "externalized_tool_results": sum(1 for event in events
            if (event.get("type") == "tool_result"
                and event.get("externalized") is True)
            or event.get("type") == "tool_result_externalized"
            or (event.get("type") == "task_notification"
                and event.get("externalized") is True)),
        "exact_repeated_test_commands": sum(
            count - 1 for count in test_counts.values() if count > 1),
        "targeted_test_commands": sum(1 for command in test_commands
            if "::" in command or " -k " in f" {command} " or "test_" in command),
        "context_integrity": [
            {key: event.get(key) for key in (
                "checkpoint_present", "latest_user_preserved",
                "orphan_tool_result_ids", "message_count")}
            for event in events if event.get("type") == "context_integrity"
        ],
        "task_events": dict(Counter(event.get("type") for event in events
            if str(event.get("type", "")).startswith("shared_task_"))),
        "message_bus_events": dict(Counter(event.get("type") for event in events
            if str(event.get("type", "")).startswith("message_bus_"))),
        "worktree_events": dict(Counter(event.get("type") for event in events
            if str(event.get("type", "")).startswith("worktree_"))),
        "teammate_model_calls": dict(teammate_model_calls),
        "teammate_tool_calls": dict(teammate_tool_calls),
        "unreceived_message_signatures": sum((sent - received).values()),
        "duplicate_message_sends": sum(
            count - 1 for count in message_signatures.values() if count > 1),
        "message_latency_max_sec": max(message_latencies, default=0.0),
        "dependency_claim_rejections": sum(1 for event in events
            if event.get("type") == "shared_task_claim_rejected"
            and event.get("reason") == "dependencies"),
        "worktree_integration_failures": sum(1 for event in events
            if event.get("type") == "worktree_integration_failed"),
        "worktree_cleanup_count": sum(1 for event in events
            if event.get("type") == "worktree_removed"),
        "teammate_empty_turns": sum(1 for event in events
            if event.get("type") == "teammate_turn"
            and int(event.get("tool_use_count") or 0) == 0),
    }


def run_stress_grader(
    *,
    case_file: str,
    package: str,
    implementation_files,
    protected_files,
    expected_architecture,
    outcome_groups,
    process_validator=None,
    process_points: float = 0,
) -> int:
    parser = argparse.ArgumentParser()
    for name in ("workspace", "trace", "final", "stdout", "stderr"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    case_root = Path(case_file).resolve().parent
    hidden = case_root / "grader_tests"
    functional, failed, results, all_runs = 0.0, [], {}, []
    for name, (points, filename) in outcome_groups.items():
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
    protected = _protected_changes(
        workspace, case_root / "workspace", protected_files)
    if api_ok:
        functional += 2.5
    if not protected:
        functional += 2.5
    quality, quality_metrics = _quality(
        workspace, package, implementation_files, expected_architecture)
    process_ok = True
    process_metrics = {}
    process_failures = []
    if process_validator is not None:
        process_ok, process_metrics, process_failures = process_validator(
            trace_events(Path(args.trace)))
        if process_ok:
            functional += process_points
    passed = (not failed and api_ok and not protected and quality == 20
              and process_ok)
    reasons = []
    if failed:
        reasons.append("failed outcome groups: " + ", ".join(failed))
    if not api_ok:
        reasons.append("public API compatibility failed")
    if protected:
        reasons.append("protected files changed: " + ", ".join(protected))
    if quality < 20:
        reasons.append("deterministic source quality checks failed")
    if not process_ok:
        reasons.append("collaboration process failed: "
                       + ", ".join(process_failures))
    if protected or not api_ok or not process_ok:
        category = "constraint_violation"
    elif any(run.get("timed_out") for run in all_runs):
        category = "test_timeout"
    else:
        category = "test_failure"
    breakdown = {
        "functional_correctness": functional,
        "code_quality": quality,
        "runtime_efficiency": 0,
        "token_cost": 0,
    }
    return emit_result(
        passed=passed,
        reason="; ".join(reasons),
        failure_category=None if passed else category,
        metrics={
            **_process_metrics(Path(args.trace)),
            "outcome_groups": results,
            "failed_outcome_groups": failed,
            "protected_changes": protected,
            "code_quality": quality_metrics,
            "collaboration_process": process_metrics,
            "collaboration_process_failures": process_failures,
            "dimension_points": breakdown,
        },
        breakdown=breakdown,
    )
