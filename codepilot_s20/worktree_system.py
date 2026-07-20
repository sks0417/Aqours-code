from .runtime_state import *

# ── Worktree System ──

# Worktree names become filesystem paths, so the teaching version keeps the
# validation rules strict and reuses them for create/remove/keep.
WORKTREES_DIR = WORKDIR / ".worktrees"

VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def validate_worktree_name(name: str) -> str | None:
    if not name:
        return "Worktree name cannot be empty"
    if name in (".", ".."):
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None


def run_git(args: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(["git"] + args, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out[:5000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"


def _run_git_at(cwd: Path, args: list[str], timeout: float = 30):
    try:
        return subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None


def _git_lines(cwd: Path, args: list[str]) -> list[str]:
    result = _run_git_at(cwd, args)
    if result is None or result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def log_event(event_type: str, worktree_name: str, task_id: str = ""):
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    event = {"type": event_type, "worktree": worktree_name,
             "task_id": task_id, "ts": time.time()}
    events_file = WORKTREES_DIR / "events.jsonl"
    with open(events_file, "a") as f:
        f.write(json.dumps(event) + "\n")


def create_worktree(name: str, task_id: str = "") -> str:
    # Tool-layer validation is part of the safety boundary; do it before git
    # sees the name, not only after git happens to reject something.
    err = validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    if task_id:
        try:
            load_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKTREES_DIR / name
    if path.exists():
        return f"Worktree '{name}' already exists at {path}"
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    log_event("create", name, task_id)
    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path}"


def bind_task_to_worktree(task_id: str, worktree_name: str):
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)


def _count_worktree_changes(path: Path) -> tuple[int, int]:
    try:
        r1 = subprocess.run(["git", "status", "--porcelain"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
        main_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=WORKDIR,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        r2 = subprocess.run(["git", "rev-list", "--count", f"{main_head}..HEAD"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        commits = int(r2.stdout.strip() or "0") if r2.returncode == 0 else 0
        return files, commits
    except Exception:
        return -1, -1


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"Worktree '{name}' not found"
    if not discard_changes:
        files, commits = _count_worktree_changes(path)
        if files < 0:
            return "Cannot verify status. Use discard_changes=true to force."
        if files > 0 or commits > 0:
            return (f"Worktree '{name}' has {files} file(s), {commits} commit(s). "
                    "Use discard_changes=true or keep_worktree.")
    ok1, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok1:
        return f"Failed to remove worktree '{name}'"
    run_git(["branch", "-D", f"wt/{name}"])
    log_event("remove", name)
    print(f"  \033[33m[worktree] removed: {name}\033[0m")
    return f"Worktree '{name}' removed"


def keep_worktree(name: str) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    log_event("keep", name)
    return f"Worktree '{name}' kept for review (branch: wt/{name})"


def finalize_worktree(name: str, commit_message: str = "worker delegated change") -> str:
    """Commit a delegated worker's isolated changes for controlled integration."""
    err = validate_worktree_name(name)
    if err:
        return json.dumps({"status": "error", "error": err})
    path = WORKTREES_DIR / name
    if not path.exists():
        return json.dumps({"status": "error", "error": "worktree not found"})

    status = _run_git_at(path, ["status", "--porcelain"])
    if status is None:
        return json.dumps({"status": "error", "error": "git status timeout"})
    if status.returncode != 0:
        return json.dumps({
            "status": "error",
            "error": (status.stderr or status.stdout).strip()[:2000],
        })
    changed_files = [
        line[3:].strip() for line in status.stdout.splitlines() if line.strip()
    ]
    if not changed_files:
        return json.dumps({
            "status": "no_changes", "worktree": name,
            "commit": "", "changed_files": [],
        })

    added = _run_git_at(path, ["add", "-A"])
    if added is None or added.returncode != 0:
        detail = "git add timeout" if added is None else added.stderr.strip()
        return json.dumps({"status": "error", "error": detail[:2000]})
    committed = _run_git_at(path, [
        "-c", "user.name=CodePilot Worker",
        "-c", "user.email=worker@localhost",
        "commit", "-m", str(commit_message or "worker delegated change")[:200],
    ])
    if committed is None or committed.returncode != 0:
        detail = "git commit timeout" if committed is None else (
            committed.stderr or committed.stdout).strip()
        return json.dumps({"status": "error", "error": detail[:2000]})
    commit = _git_lines(path, ["rev-parse", "HEAD"])
    diff_stat = _git_lines(path, ["show", "--stat", "--oneline", "--format=", "HEAD"])
    log_event("worker_commit", name)
    return json.dumps({
        "status": "changes_ready", "worktree": name,
        "commit": commit[0] if commit else "",
        "changed_files": changed_files,
        "diff_stat": diff_stat[:50],
    })


def integrate_worktree(name: str, cleanup: bool = True) -> str:
    """Merge a finalized worker branch without overwriting lead-owned changes."""
    err = validate_worktree_name(name)
    if err:
        return json.dumps({"status": "error", "error": err})
    path = WORKTREES_DIR / name
    branch = f"wt/{name}"
    if not path.exists():
        return json.dumps({"status": "error", "error": "worktree not found"})

    worker_status = _run_git_at(path, ["status", "--porcelain"])
    if worker_status is None or worker_status.returncode != 0:
        return json.dumps({"status": "error", "error": "cannot inspect worker"})
    if worker_status.stdout.strip():
        return json.dumps({
            "status": "error",
            "error": "worker worktree has uncommitted changes; finalize it first",
        })

    ahead = _git_lines(WORKDIR, ["rev-list", "--count", f"HEAD..{branch}"])
    if not ahead:
        return json.dumps({"status": "error", "error": "worker branch not found"})
    if int(ahead[0]) == 0:
        cleanup_result = remove_worktree(name, discard_changes=True) if cleanup else ""
        return json.dumps({
            "status": "no_changes", "worktree": name,
            "changed_files": [], "cleanup": cleanup_result,
        })

    changed_files = _git_lines(WORKDIR, ["diff", "--name-only", f"HEAD...{branch}"])
    dirty_files = set(_git_lines(WORKDIR, ["diff", "--name-only"]))
    dirty_files.update(_git_lines(WORKDIR, ["diff", "--cached", "--name-only"]))
    overlap = sorted(set(changed_files) & dirty_files)
    if overlap:
        return json.dumps({
            "status": "conflict", "worktree": name,
            "error": "lead and worker changed the same files",
            "overlapping_files": overlap,
        })

    merged = _run_git_at(WORKDIR, [
        "-c", "user.name=CodePilot Lead",
        "-c", "user.email=lead@localhost",
        "merge", "--no-ff", "--no-edit", branch,
    ], timeout=60)
    if merged is None or merged.returncode != 0:
        merge_head = _run_git_at(WORKDIR, ["rev-parse", "-q", "--verify", "MERGE_HEAD"])
        if merge_head is not None and merge_head.returncode == 0:
            _run_git_at(WORKDIR, ["merge", "--abort"])
        detail = "git merge timeout" if merged is None else (
            merged.stderr or merged.stdout).strip()
        return json.dumps({
            "status": "conflict", "worktree": name,
            "error": detail[:2000], "changed_files": changed_files,
        })

    commit = _git_lines(WORKDIR, ["rev-parse", "HEAD"])
    log_event("integrate", name)
    cleanup_result = remove_worktree(name, discard_changes=True) if cleanup else ""
    return json.dumps({
        "status": "integrated", "worktree": name,
        "commit": commit[0] if commit else "",
        "changed_files": changed_files,
        "cleanup": cleanup_result,
    })



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
