from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stress_grader import run_stress_grader


if __name__ == "__main__":
    raise SystemExit(run_stress_grader(
        case_file=__file__, package="notification_dispatcher",
        implementation_files=(
            "src/notification_dispatcher/__init__.py", "src/notification_dispatcher/api.py",
            "src/notification_dispatcher/bootstrap.py", "src/notification_dispatcher/dedupe.py",
            "src/notification_dispatcher/errors.py", "src/notification_dispatcher/models.py",
            "src/notification_dispatcher/policy.py", "src/notification_dispatcher/providers.py",
            "src/notification_dispatcher/rate_limit.py", "src/notification_dispatcher/serialization.py",
            "src/notification_dispatcher/service.py", "src/notification_dispatcher/validation.py",
        ),
        protected_files=("README.md", "docs/delivery_contract.md", "docs/operations.md",
                         "pyproject.toml", "tests/conftest.py", "tests/test_public_delivery.py"),
        expected_architecture={
            "api.py": {"NotificationAPI"}, "bootstrap.py": {"NotificationApplication", "build_application"},
            "dedupe.py": {"DedupeRepository"}, "policy.py": {"ChannelPolicy"},
            "providers.py": {"ScriptedProvider", "ProviderRegistry"},
            "rate_limit.py": {"RecipientRateLimiter"}, "service.py": {"NotificationService"},
        },
        outcome_groups={
            "fallback_contract": (15, "test_fallback_contract.py"),
            "failure_boundaries": (15, "test_failure_boundaries.py"),
            "idempotency": (10, "test_idempotency.py"),
        },
    ))
