from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stress_grader import run_stress_grader


if __name__ == "__main__":
    raise SystemExit(run_stress_grader(
        case_file=__file__, package="inventory_import_pipeline",
        implementation_files=(
            "src/inventory_import_pipeline/__init__.py", "src/inventory_import_pipeline/api.py",
            "src/inventory_import_pipeline/bootstrap.py", "src/inventory_import_pipeline/dedupe.py",
            "src/inventory_import_pipeline/errors.py", "src/inventory_import_pipeline/models.py",
            "src/inventory_import_pipeline/parser.py", "src/inventory_import_pipeline/repository.py",
            "src/inventory_import_pipeline/serialization.py", "src/inventory_import_pipeline/service.py",
            "src/inventory_import_pipeline/validation.py",
        ),
        protected_files=("README.md", "docs/import_contract.md", "pyproject.toml",
                         "tests/test_public_happy_path.py",
                         "tests/test_public_verbose_duplicate_report.py"),
        expected_architecture={
            "api.py": {"InventoryImportAPI"},
            "bootstrap.py": {"InventoryImportApplication", "build_application"},
            "dedupe.py": {"ImportDedupeRepository"},
            "repository.py": {"InventoryRepository"},
            "service.py": {"InventoryImportService"},
            "validation.py": {"validate_rows"},
        },
        outcome_groups={
            "duplicate_semantics": (15, "test_duplicate_semantics.py"),
            "retry_atomicity": (15, "test_retry_atomicity.py"),
            "validation": (10, "test_validation.py"),
        },
    ))
