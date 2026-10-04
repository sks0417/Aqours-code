"""Git operations for running a graph. This is the only module that calls git."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

INTEGRATION_BRANCH = "tg/integration"
NODE_BRANCH_PREFIX = "tg/node/"
USER_NAME = "aqours-taskgraph"
USER_EMAIL = "taskgraph@localhost"
# Aqours runtime state and Python caches that workers may leave in a worktree.
RUNTIME_EXCLUDES = (
    ".aqours_code/", ".task_outputs/", ".transcripts/", ".memory/", ".tasks/",
    ".mailboxes/", ".worktrees/", ".scheduled_tasks.json",
    ".scheduled_once_tasks.json", "__pycache__/", ".pytest_cache/", "*.pyc",
)


class GitError(RuntimeError):
    """A git command failed."""


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``git`` in ``cwd`` and return the completed process."""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              check=False)
    except OSError as exc:
        raise GitError(f"cannot run git: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return proc


def head(cwd: Path, ref: str = "HEAD") -> str:
    """Return the commit id of ``ref``."""
    return git(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def clone_for_run(source: Path, dest: Path, base_commit: str) -> str:
    """Clone ``source`` into ``dest`` and check out the integration branch.

    Returns the resolved base commit.
    """
    git(dest.parent, "clone", "-q", "--no-checkout", "-c", "core.autocrlf=false",
        "-c", "commit.gpgsign=false", str(source), dest.name)
    git(dest, "config", "user.name", USER_NAME)
    git(dest, "config", "user.email", USER_EMAIL)
    base = head(dest, base_commit)
    git(dest, "checkout", "-q", "-b", INTEGRATION_BRANCH, base)
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = "\n".join(RUNTIME_EXCLUDES)
    exclude.write_text(f"{existing.rstrip()}\n# aqours taskgraph\n{lines}\n".lstrip(),
                       encoding="utf-8")
    return base


def add_node_worktree(repo: Path, node_id: str, path: Path) -> str:
    """Create branch ``tg/node/<id>`` and its worktree at the integration HEAD.

    Returns the start commit.
    """
    start = head(repo, INTEGRATION_BRANCH)
    git(repo, "worktree", "add", "-q", "-b", NODE_BRANCH_PREFIX + node_id,
        str(path), start)
    return start


def add_detached_worktree(repo: Path, path: Path, ref: str) -> None:
    """Check out ``ref`` into a clean detached worktree at ``path``."""
    git(repo, "worktree", "add", "-q", "--detach", str(path), ref)


def remove_worktree(repo: Path, path: Path) -> None:
    """Remove a worktree, keeping its branch."""
    proc = git(repo, "worktree", "remove", "--force", str(path), check=False)
    if proc.returncode != 0 and path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


def commit_all(worktree: Path, message: str) -> bool:
    """Stage everything (respecting excludes) and commit; False if nothing changed."""
    git(worktree, "add", "-A")
    if git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git(worktree, "commit", "-q", "--no-verify", "-m", message)
    return True


def changed_files(cwd: Path, start: str, end: str = "HEAD") -> list[str]:
    """Files that differ between ``start`` and ``end``."""
    output = git(cwd, "-c", "core.quotepath=off", "diff", "--name-only", "-z",
                 start, end).stdout
    return sorted(name for name in output.split("\0") if name)


def diff_patch(cwd: Path, start: str, end: str = "HEAD") -> str:
    """Return the patch between ``start`` and ``end``."""
    return git(cwd, "diff", "--binary", start, end).stdout


def squash_merge(repo: Path, branch: str, message: str) -> str | None:
    """Squash-merge ``branch`` into the checked-out branch as one commit.

    Returns the new commit, or None after a conflict (the branch is restored).
    """
    proc = git(repo, "merge", "--squash", branch, check=False)
    if proc.returncode != 0:
        git(repo, "reset", "-q", "--hard", "HEAD")
        return None
    git(repo, "commit", "-q", "--no-verify", "--allow-empty", "-m", message)
    return head(repo)


def undo_last_commit(repo: Path) -> None:
    """Drop the last commit of the checked-out branch and its changes."""
    git(repo, "reset", "-q", "--hard", "HEAD~1")


def commit_subjects(repo: Path, start: str, end: str = "HEAD") -> list[str]:
    """Commit subjects from ``start`` (exclusive) to ``end``, oldest first."""
    output = git(repo, "log", "--reverse", "--format=%s", f"{start}..{end}").stdout
    return [line for line in output.splitlines() if line]


def is_clean(repo: Path) -> bool:
    """True when the working tree has no tracked or untracked changes."""
    return not git(repo, "status", "--porcelain").stdout.strip()
