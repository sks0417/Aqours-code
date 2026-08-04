from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import is_test_command  # noqa: E402
from stress_grader import run_stress_grader  # noqa: E402


OUTCOME_GROUPS = {
    "lease_fencing": (8, "test_lease_fencing.py"),
    "retry_idempotency": (8, "test_retry_idempotency.py"),
    "submission_conflicts": (8, "test_submission_conflicts.py"),
    "cancellation_state": (8, "test_cancellation_state.py"),
    "restart_recovery": (8, "test_recovery.py"),
}
IMPLEMENTATION_FILES = (
    "src/worker_queue/__init__.py",
    "src/worker_queue/api.py",
    "src/worker_queue/bootstrap.py",
    "src/worker_queue/errors.py",
    "src/worker_queue/models.py",
    "src/worker_queue/validation.py",
    "src/worker_queue/fingerprint.py",
    "src/worker_queue/serialization.py",
    "src/worker_queue/repositories.py",
    "src/worker_queue/leases.py",
    "src/worker_queue/recovery.py",
    "src/worker_queue/service.py",
)
PROTECTED_FILES = (
    "README.md",
    "pyproject.toml",
    "tests/conftest.py",
    "tests/test_public_submission.py",
    "tests/test_public_lifecycle.py",
    "tests/test_public_recovery.py",
)
EXPECTED_ARCHITECTURE = {
    "api.py": {"QueueAPI"},
    "bootstrap.py": {"QueueApplication", "build_application", "build_api"},
    "repositories.py": {
        "JobRepository", "RequestRepository", "EventRepository",
        "OperationRepository",
    },
    "leases.py": {"LeaseFence"},
    "recovery.py": {"RecoveryManager"},
    "service.py": {"QueueService"},
}


def require_test_run(events):
    observed = False
    commands: list[str] = []
    for event in events:
        if event.get("type") != "tool_use":
            continue
        data = event.get("input") if isinstance(event.get("input"), dict) else {}
        command = str(data.get("command") or data.get("cmd") or "")
        if is_test_command(command):
            observed = True
            commands.append(command.strip())
    failures = [] if observed else ["no pytest or unittest command observed"]
    return observed, {
        "test_run_observed": observed,
        "observed_test_commands": commands[-5:],
    }, failures


if __name__ == "__main__":
    raise SystemExit(run_stress_grader(
        case_file=__file__,
        package="worker_queue",
        implementation_files=IMPLEMENTATION_FILES,
        protected_files=PROTECTED_FILES,
        expected_architecture=EXPECTED_ARCHITECTURE,
        outcome_groups=OUTCOME_GROUPS,
        process_validator=require_test_run,
        process_points=0,
    ))
