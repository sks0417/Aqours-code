from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stress_grader import run_stress_grader


def validate_dependency_process(events):
    created = [e for e in events if e.get("type") == "shared_task_created"]
    dependent = next((e for e in created if e.get("blocked_by")), None)
    failures = []
    if dependent is None:
        failures.append("dependent task with blockedBy not created")
        return False, {"created_tasks": len(created)}, failures
    task_b = dependent.get("task_id")
    task_a = dependent.get("blocked_by", [None])[0]
    indexed = list(enumerate(events))
    claim_a = next(((i, e) for i, e in indexed if e.get("type") == "shared_task_claimed"
                    and e.get("task_id") == task_a), None)
    complete_a = next(((i, e) for i, e in indexed if e.get("type") == "shared_task_completed"
                       and e.get("task_id") == task_a), None)
    claim_b = next(((i, e) for i, e in indexed if e.get("type") == "shared_task_claimed"
                    and e.get("task_id") == task_b), None)
    complete_b = next(((i, e) for i, e in indexed if e.get("type") == "shared_task_completed"
                       and e.get("task_id") == task_b), None)
    worktree_a = next((e.get("worktree") for e in events
                       if e.get("type") == "worktree_task_bound" and e.get("task_id") == task_a), None)
    worktree_b = next((e.get("worktree") for e in events
                       if e.get("type") == "worktree_task_bound" and e.get("task_id") == task_b), None)
    integrate_a = next((i for i, e in indexed if e.get("type") == "worktree_integrated"
                        and e.get("worktree") == worktree_a), None)
    integrate_b = next((i for i, e in indexed if e.get("type") == "worktree_integrated"
                        and e.get("worktree") == worktree_b), None)
    finalized = {e.get("worktree") for e in events
                 if e.get("type") == "worktree_finalized"}
    handoff = next((i for i, e in indexed if e.get("type") == "message_bus_sent"
                    and claim_a and e.get("from_agent") == claim_a[1].get("owner")
                    and e.get("to_agent") == "lead"), None)
    if not all((claim_a, complete_a, claim_b, complete_b)):
        failures.append("dependency tasks were not both claimed and completed")
    elif claim_a[1].get("owner") == claim_b[1].get("owner"):
        failures.append("different teammates did not own A and B")
    if complete_a and claim_b and complete_a[0] >= claim_b[0]:
        failures.append("B claimed before A completed")
    if integrate_a is None or claim_b and integrate_a >= claim_b[0]:
        failures.append("A not integrated before B claim")
    if handoff is None or claim_b and handoff >= claim_b[0]:
        failures.append("storage handoff missing before B claim")
    if integrate_b is None:
        failures.append("B worktree not integrated")
    if len({e.get("teammate") for e in events if e.get("type") == "teammate_spawned"}) < 2:
        failures.append("two persistent teammates not spawned")
    metrics = {"task_a": task_a, "task_b": task_b, "worktree_a": worktree_a,
               "worktree_b": worktree_b, "handoff_before_b": bool(handoff is not None and claim_b and handoff < claim_b[0]),
               "a_integrated_before_b": bool(integrate_a is not None and claim_b and integrate_a < claim_b[0]),
               "finalized_via_helper": len(finalized)}
    return not failures, metrics, failures


if __name__ == "__main__":
    raise SystemExit(run_stress_grader(
        case_file=__file__, package="document_index",
        implementation_files=(
            "src/document_index/__init__.py", "src/document_index/api.py",
            "src/document_index/bootstrap.py", "src/document_index/errors.py",
            "src/document_index/migration.py", "src/document_index/models.py",
            "src/document_index/query.py", "src/document_index/serialization.py",
            "src/document_index/storage.py", "src/document_index/validation.py",
        ),
        protected_files=("README.md", "docs/storage_contract.md", "pyproject.toml",
                         "tests/test_public_index.py"),
        expected_architecture={"api.py": {"DocumentIndexAPI"},
            "bootstrap.py": {"DocumentIndexApplication", "build_application"},
            "migration.py": {"StorageMigrationService"},
            "query.py": {"DocumentQueryService"}, "storage.py": {"DocumentRepository"}},
        outcome_groups={"storage_migration": (18, "test_storage_migration.py"),
                        "query_handoff": (17, "test_query_handoff.py")},
        process_validator=validate_dependency_process, process_points=5,
    ))
