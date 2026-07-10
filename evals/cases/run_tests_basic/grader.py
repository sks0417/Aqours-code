from __future__ import annotations

import argparse
import json
import subprocess
import sys
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import emit_result, trace_contains_test_run, trace_tool_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--final", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    args = parser.parse_args()

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=args.workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )
    saw_test_run = trace_contains_test_run(args.trace)
    passed = result.returncode == 0 and saw_test_run
    error = ""
    if not passed:
        error = (result.stdout + result.stderr).strip() or "pytest did not pass or test run was not traced"
    return emit_result(
        passed=passed,
        reason=error,
        failure_category="test_failure",
        metrics={"tool_calls": trace_tool_count(args.trace), "saw_test_run": saw_test_run},
    )


if __name__ == "__main__":
    raise SystemExit(main())
