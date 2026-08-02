from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stress_grader import run_stress_grader


def validate_parallel_process(events):
    created = {e.get("task_id") for e in events if e.get("type") == "shared_task_created"}
    worktrees = {e.get("worktree") for e in events if e.get("type") == "worktree_created"}
    bound = {e.get("task_id") for e in events if e.get("type") == "worktree_task_bound"}
    claims = [(e.get("task_id"), e.get("owner")) for e in events
              if e.get("type") == "shared_task_claimed"]
    completed = {e.get("task_id") for e in events if e.get("type") == "shared_task_completed"}
    integrated = {e.get("worktree") for e in events if e.get("type") == "worktree_integrated"}
    finalized = {e.get("worktree") for e in events if e.get("type") == "worktree_finalized"}
    teammates = {e.get("teammate") for e in events if e.get("type") == "teammate_spawned"}
    handoffs = [e for e in events if e.get("type") == "message_bus_sent"
                and e.get("to_agent") == "lead" and e.get("from_agent") != "lead"]
    failures = []
    if len(created) < 2: failures.append("two shared tasks not created")
    if len(worktrees) < 2 or len(bound & created) < 2: failures.append("two tasks not bound to worktrees")
    claim_tasks = {task for task, owner in claims if owner and owner != "lead"}
    claim_owners = {owner for task, owner in claims if task in created and owner != "lead"}
    if len(claim_tasks) < 2 or len(claim_owners) < 2: failures.append("tasks not claimed by different teammates")
    if len(completed & created) < 2: failures.append("two tasks not completed")
    if len(integrated) < 2: failures.append("two worktrees not integrated")
    if len(teammates) < 2: failures.append("two persistent teammates not spawned")
    if len(handoffs) < 2: failures.append("teammate handoff messages missing")
    metrics = {"created_tasks": len(created), "worktrees": len(worktrees),
               "claim_owners": sorted(claim_owners), "completed_tasks": len(completed),
               "integrated_worktrees": len(integrated), "teammates": len(teammates),
               # integrate_worktree only succeeds for a committed branch;
               # finalize events are additionally diagnostic because workers
               # may commit with ordinary Git commands in their Worktree.
               "finalized_via_helper": len(finalized),
               "handoff_messages": len(handoffs)}
    return not failures, metrics, failures


if __name__ == "__main__":
    raise SystemExit(run_stress_grader(
        case_file=__file__, package="billing_export",
        implementation_files=(
            "src/billing_export/__init__.py", "src/billing_export/api.py",
            "src/billing_export/bootstrap.py", "src/billing_export/common.py",
            "src/billing_export/errors.py", "src/billing_export/models.py",
            "src/billing_export/csv_export/__init__.py", "src/billing_export/csv_export/encoder.py",
            "src/billing_export/csv_export/service.py", "src/billing_export/json_export/__init__.py",
            "src/billing_export/json_export/encoder.py", "src/billing_export/json_export/service.py",
        ),
        protected_files=("README.md", "docs/export_contract.md", "pyproject.toml",
                         "tests/test_public_export.py"),
        expected_architecture={"api.py": {"BillingExportAPI"},
            "bootstrap.py": {"BillingExportApplication", "build_application"},
            "csv_export/encoder.py": {"CSVInvoiceEncoder"},
            "csv_export/service.py": {"CSVExportService"},
            "json_export/encoder.py": {"JSONInvoiceEncoder"},
            "json_export/service.py": {"JSONExportService"}},
        outcome_groups={"csv_migration": (18, "test_csv_migration.py"),
                        "json_migration": (17, "test_json_migration.py")},
        process_validator=validate_parallel_process, process_points=5,
    ))
