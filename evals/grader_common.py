from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


BREAKDOWN_WEIGHTS = {
    "outcome_correctness": 40,
    "constraints": 15,
    "process_quality": 20,
    "code_quality": 15,
    "efficiency": 10,
}


def make_breakdown(passed: bool, overrides: dict | None = None) -> dict:
    if passed:
        breakdown = dict(BREAKDOWN_WEIGHTS)
    else:
        breakdown = {key: 0 for key in BREAKDOWN_WEIGHTS}
    for key, value in (overrides or {}).items():
        if key in BREAKDOWN_WEIGHTS:
            breakdown[key] = max(0, min(BREAKDOWN_WEIGHTS[key], float(value)))
    return breakdown


def emit_result(
    *,
    passed: bool,
    reason: str = "",
    failure_category: str | None = None,
    metrics: dict | None = None,
    breakdown: dict | None = None,
) -> int:
    final_breakdown = make_breakdown(passed, breakdown)
    payload = {
        "passed": passed,
        "score": sum(final_breakdown.values()),
        "breakdown": final_breakdown,
        "metrics": metrics or {},
        "reason": "" if passed else reason,
        "failure_category": None if passed else (failure_category or "grader_error"),
    }
    print(json.dumps(payload))
    return 0 if passed else 1


def trace_events(trace_path: str | Path) -> list[dict]:
    path = Path(trace_path)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def trace_tool_count(trace_path: str | Path) -> int:
    return sum(1 for event in trace_events(trace_path) if event.get("type") == "tool_use")


def trace_contains_test_run(trace_path: str | Path) -> bool:
    for event in trace_events(trace_path):
        if event.get("type") != "tool_use":
            continue
        text = json.dumps(event.get("input", {}), ensure_ascii=False).lower()
        if "pytest" in text or "test" in text:
            return True
    return False


def run_pytest(
    workspace: str | Path,
    test_paths: list[str | Path],
    *,
    timeout: float = 60,
) -> dict:
    workspace = Path(workspace).resolve()
    resolved_tests = []
    for test_path in test_paths:
        path = Path(test_path)
        if not path.is_absolute():
            path = workspace / path
        resolved_tests.append(str(path.resolve()))
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["EVAL_GRADING_WORKSPACE"] = str(workspace)
    cmd = [sys.executable, "-m", "pytest", "-q", *resolved_tests]
    try:
        proc = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return {
            "command": cmd,
            "test_paths": resolved_tests,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timed_out": False,
            "failure_category": "test_failure" if proc.returncode else None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "test_paths": resolved_tests,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
            "failure_category": "test_timeout",
        }
