from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import is_test_command  # noqa: E402
from stress_grader import run_stress_grader  # noqa: E402


OUTCOME_GROUPS = {
    "cache_key_correctness": (5, "test_cache_key_correctness.py"),
    "writer_fencing_concurrency": (8, "test_writer_fencing.py"),
    "atomic_publication": (6, "test_atomic_publication.py"),
    "artifact_integrity": (5, "test_artifact_integrity.py"),
    "crash_recovery": (6, "test_crash_recovery.py"),
    "manifest_compatibility": (4, "test_manifest_compatibility.py"),
    "failure_path_idempotency": (6, "test_failure_idempotency.py"),
}
IMPLEMENTATION_FILES = (
    "src/artifact_cache/key.py",
    "src/artifact_cache/lock.py",
    "src/artifact_cache/manifest.py",
    "src/artifact_cache/store.py",
    "src/artifact_cache/recovery.py",
    "src/artifact_cache/service.py",
)
PROTECTED_FILES = (
    "README.md",
    "pyproject.toml",
    "src/artifact_cache/__init__.py",
    "src/artifact_cache/models.py",
    "tests/conftest.py",
    "tests/test_public_key_semantics.py",
    "tests/test_public_cache.py",
    "tests/test_public_integrity.py",
    "tests/test_public_publication.py",
    "tests/test_public_recovery.py",
)
EXPECTED_ARCHITECTURE = {
    "key.py": {"cache_key"},
    "lock.py": {"LeaseRegistry"},
    "manifest.py": {"artifact_digest", "build_manifest", "read_manifest", "write_manifest"},
    "store.py": {"CacheStore"},
    "recovery.py": {"RecoveryManager"},
    "service.py": {"ArtifactCache"},
}


def require_test_run(events):
    commands: list[str] = []
    for event in events:
        if event.get("type") != "tool_use":
            continue
        data = event.get("input") if isinstance(event.get("input"), dict) else {}
        command = str(data.get("command") or data.get("cmd") or "")
        if is_test_command(command):
            commands.append(command.strip())
    observed = bool(commands)
    return observed, {
        "test_run_observed": observed,
        "observed_test_commands": commands[-5:],
    }, ([] if observed else ["no pytest or unittest command observed"])


if __name__ == "__main__":
    raise SystemExit(run_stress_grader(
        case_file=__file__,
        package="artifact_cache",
        implementation_files=IMPLEMENTATION_FILES,
        protected_files=PROTECTED_FILES,
        expected_architecture=EXPECTED_ARCHITECTURE,
        outcome_groups=OUTCOME_GROUPS,
        process_validator=require_test_run,
        process_points=0,
    ))

