from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    from .scoring import BREAKDOWN_WEIGHTS
except ImportError:  # Trusted grader copy imports modules from its root.
    from scoring import BREAKDOWN_WEIGHTS


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
        tool_name = str(event.get("tool") or event.get("name") or "").lower()
        tool_input = event.get("input", {}) if isinstance(event.get("input"), dict) else {}
        command = str(tool_input.get("command") or tool_input.get("cmd") or "").lower()
        if tool_name and tool_name not in {"bash", "shell", "powershell", "cmd"}:
            continue
        if not command:
            continue
        if is_test_command(command):
            return True
    return False


def is_test_command(command: str) -> bool:
    command = str(command).lower()
    patterns = [
        r"(^|[;&|]\s*|\&\&\s*)\"?[^\";&|]*\b(python|python3|py)(\.exe)?\"?(\s+-u)?\s+-m\s+pytest\b",
        r"(^|[;&|]\s*|\&\&\s*)\"?[^\";&|]*\b(python|python3|py)(\.exe)?\"?(\s+-u)?\s+-m\s+unittest\b",
        r"(^|[;&|]\s*|\&\&\s*)\"?[^\";&|]*\bpytest(\.exe)?\"?\b",
    ]
    return any(re.search(pattern, command) for pattern in patterns)


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
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["EVAL_GRADING_WORKSPACE"] = str(workspace)
    env.pop("PYTHONPATH", None)
    # Run trusted and workspace test paths independently. This avoids pytest
    # selecting their filesystem common ancestor ("/" in Docker or a user
    # profile on Windows) as a collection root and inspecting unrelated files.
    commands = []
    stdout_parts = []
    stderr_parts = []
    returncode = 0
    deadline = time.monotonic() + timeout
    for test_path in resolved_tests:
        test_path_obj = Path(test_path)
        try:
            test_path_obj.relative_to(workspace)
            test_root = workspace
        except ValueError:
            test_root = test_path_obj.parent
        cmd = [
            sys.executable, "-m", "pytest", "-q",
            "-p", "no:cacheprovider",
            "--rootdir", str(test_root),
            "--confcutdir", str(test_root),
            test_path,
        ]
        commands.append(cmd)
        remaining = max(0.001, deadline - time.monotonic())
        try:
            proc = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=remaining,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_parts.append(exc.stdout or "")
            stderr_parts.append(exc.stderr or "")
            return {
                "command": commands,
                "test_paths": resolved_tests,
                "returncode": None,
                "stdout": "\n".join(stdout_parts),
                "stderr": "\n".join(stderr_parts),
                "timed_out": True,
                "failure_category": "test_timeout",
            }
        stdout_parts.append(proc.stdout or "")
        stderr_parts.append(proc.stderr or "")
        if proc.returncode and returncode == 0:
            returncode = proc.returncode
    return {
        "command": commands,
        "test_paths": resolved_tests,
        "returncode": returncode,
        "stdout": "\n".join(stdout_parts),
        "stderr": "\n".join(stderr_parts),
        "timed_out": False,
        "failure_category": "test_failure" if returncode else None,
    }
