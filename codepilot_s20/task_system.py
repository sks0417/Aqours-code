from .runtime_state import *

# ── Task System ──

# Tasks are tiny durable records. Later systems add ownership, dependencies,
# worktrees, and teammates on top of this same file-backed state.
TASKS_DIR = WORKDIR / ".tasks"
CURRENT_TODOS: list[dict] = []


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    record_event("shared_task_created", task_id=task.id,
                 subject=task.subject, blocked_by=list(task.blockedBy))
    return task


def save_task(task: Task):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task_json(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), indent=2)


def can_start(task_id: str) -> bool:
    # Dependencies are intentionally simple: every blocker must exist and be
    # completed before the task can be claimed.
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        record_event("shared_task_claim_rejected", task_id=task_id,
                     owner=owner, reason=f"status:{task.status}")
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:
        record_event("shared_task_claim_rejected", task_id=task_id,
                     owner=owner, reason=f"owned:{task.owner}")
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if _task_path(d).exists() and load_task(d).status != "completed"]
        missing = [d for d in task.blockedBy if not _task_path(d).exists()]
        record_event("shared_task_claim_rejected", task_id=task_id,
                     owner=owner, reason="dependencies", blocked_by=deps,
                     missing_dependencies=missing)
        parts = []
        if deps: parts.append(f"blocked by: {deps}")
        if missing: parts.append(f"missing deps: {missing}")
        return "Cannot start — " + ", ".join(parts)
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    record_event("shared_task_claimed", task_id=task.id, owner=owner,
                 subject=task.subject, worktree=task.worktree or "")
    print(f"  \033[36m[claim] {task.subject} -> in_progress\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        record_event("shared_task_complete_rejected", task_id=task_id,
                     owner=task.owner or "", reason=f"status:{task.status}")
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    record_event("shared_task_completed", task_id=task.id,
                 owner=task.owner or "", subject=task.subject,
                 worktree=task.worktree or "")
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} done\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    return msg



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
