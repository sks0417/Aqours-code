from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stress_grader import run_stress_grader  # noqa: E402


OUTCOME_GROUPS = {
    "recurrence_and_timezone": (10, "test_recurrence_timezone.py"),
    "conflicts_and_overrides": (10, "test_conflicts_overrides.py"),
    "booking_exactly_once": (10, "test_booking_exactly_once.py"),
    "creation_atomicity": (10, "test_creation_atomicity.py"),
}
IMPLEMENTATION_FILES = (
    "src/series_scheduler/__init__.py", "src/series_scheduler/api.py",
    "src/series_scheduler/bootstrap.py", "src/series_scheduler/errors.py",
    "src/series_scheduler/models.py", "src/series_scheduler/validation.py",
    "src/series_scheduler/timezones.py", "src/series_scheduler/recurrence.py",
    "src/series_scheduler/conflicts.py", "src/series_scheduler/fingerprint.py",
    "src/series_scheduler/serialization.py",
    "src/series_scheduler/repositories.py", "src/series_scheduler/service.py",
)
PROTECTED_FILES = (
    "README.md", "pyproject.toml", "tests/conftest.py",
    "tests/test_public_recurrence.py", "tests/test_public_booking.py",
)
EXPECTED_ARCHITECTURE = {
    "api.py": {"SchedulerAPI"},
    "bootstrap.py": {"SchedulerApplication", "build_application", "build_api"},
    "repositories.py": {
        "ResourceRepository", "SeriesRepository", "OccurrenceRepository",
        "CreationRepository", "RequestRepository", "BookingRepository",
        "BookingIdSequence",
    },
    "service.py": {"SchedulerService"},
}


if __name__ == "__main__":
    raise SystemExit(run_stress_grader(
        case_file=__file__,
        package="series_scheduler",
        implementation_files=IMPLEMENTATION_FILES,
        protected_files=PROTECTED_FILES,
        expected_architecture=EXPECTED_ARCHITECTURE,
        outcome_groups=OUTCOME_GROUPS,
    ))
