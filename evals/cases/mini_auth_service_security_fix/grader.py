from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import emit_result, run_pytest, trace_contains_test_run, trace_tool_count


EXPECTED_TESTS = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auth_service import authenticate, role_for


def test_valid_password_authenticates_user():
    assert authenticate("alice", "wonderland") is True


def test_wrong_password_is_rejected():
    assert authenticate("alice", "wrong") is False


def test_empty_password_is_rejected_for_existing_user():
    assert authenticate("alice", "") is False


def test_unknown_user_is_rejected():
    assert authenticate("mallory", "wonderland") is False


def test_role_lookup_still_works():
    assert role_for("bob") == "user"
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--final", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    args = parser.parse_args()

    workspace = Path(args.workspace)
    tests_unchanged = (workspace / "tests" / "test_auth_service.py").read_text(encoding="utf-8") == EXPECTED_TESTS
    grader_tests = Path(__file__).parent / "grader_tests" / "test_auth_service_grader.py"
    result = run_pytest(workspace, ["tests/test_auth_service.py", grader_tests])
    saw_test_run = trace_contains_test_run(args.trace)
    passed = tests_unchanged and result["returncode"] == 0 and saw_test_run
    if passed:
        reason = ""
        category = None
    elif not tests_unchanged:
        reason = "tests/test_auth_service.py was modified"
        category = "constraint_violation"
    elif not saw_test_run:
        reason = "trace did not show a test run"
        category = "test_failure"
    else:
        reason = (result["stdout"] + result["stderr"]).strip()
        category = result["failure_category"] or "test_failure"
    return emit_result(
        passed=passed,
        reason=reason,
        failure_category=category,
        metrics={"tool_calls": trace_tool_count(args.trace), "saw_test_run": saw_test_run, "pytest": result},
    )


if __name__ == "__main__":
    raise SystemExit(main())
