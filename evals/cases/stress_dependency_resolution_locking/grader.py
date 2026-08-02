from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stress_grader import run_stress_grader  # noqa: E402


OUTCOME_GROUPS = {
    "semver_and_constraints": (10, "test_semver_constraints.py"),
    "backtracking_and_graph": (10, "test_solver_backtracking_graph.py"),
    "features_and_conflicts": (10, "test_features_conflicts.py"),
    "lock_cache_and_replacement": (10, "test_lock_cache_replacement.py"),
}
IMPLEMENTATION_FILES = (
    "src/dependency_resolver/__init__.py",
    "src/dependency_resolver/api.py",
    "src/dependency_resolver/bootstrap.py",
    "src/dependency_resolver/errors.py",
    "src/dependency_resolver/models.py",
    "src/dependency_resolver/validation.py",
    "src/dependency_resolver/semver.py",
    "src/dependency_resolver/constraints.py",
    "src/dependency_resolver/graph.py",
    "src/dependency_resolver/fingerprint.py",
    "src/dependency_resolver/serialization.py",
    "src/dependency_resolver/repositories.py",
    "src/dependency_resolver/resolver.py",
    "src/dependency_resolver/service.py",
)
PROTECTED_FILES = (
    "README.md", "pyproject.toml", "tests/conftest.py",
    "tests/test_public_resolution.py", "tests/test_public_locking.py",
)
EXPECTED_ARCHITECTURE = {
    "api.py": {"ResolverAPI"},
    "bootstrap.py": {"ResolverApplication", "build_application", "build_api"},
    "repositories.py": {"RegistryRepository", "ResolutionCache"},
    "resolver.py": {"DependencySolver"},
    "service.py": {"ResolverService"},
}


if __name__ == "__main__":
    raise SystemExit(run_stress_grader(
        case_file=__file__,
        package="dependency_resolver",
        implementation_files=IMPLEMENTATION_FILES,
        protected_files=PROTECTED_FILES,
        expected_architecture=EXPECTED_ARCHITECTURE,
        outcome_groups=OUTCOME_GROUPS,
    ))
