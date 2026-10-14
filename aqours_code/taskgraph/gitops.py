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


def _git_bytes(cwd: Path, *args: str, stdin: bytes | None = None
               ) -> subprocess.CompletedProcess[bytes]:
    """Run ``git`` in ``cwd`` without decoding its output."""
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              input=stdin, check=False)
    except OSError as exc:
        raise GitError(f"cannot run git: {exc}") from exc


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


def add_node_worktree(repo: Path, node_id: str, path: Path, start: str) -> None:
    """Create branch ``tg/node/<id>`` and its worktree at commit ``start``."""
    git(repo, "worktree", "add", "-q", "-b", NODE_BRANCH_PREFIX + node_id,
        str(path), start)


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


def uncommitted_files(worktree: Path) -> list[str]:
    """Files of ``worktree`` that differ from its last commit, and new files.

    Ignored files are left out; a deleted file is listed although it is gone.
    """
    names: set[str] = set()
    for args in (("diff", "--name-only", "-z", "HEAD"),
                 ("ls-files", "--others", "--exclude-standard", "-z")):
        output = git(worktree, "-c", "core.quotepath=off", *args).stdout
        names.update(name for name in output.split("\0") if name)
    return sorted(names)


def blob_at(repo: Path, commit: str, path: str) -> bytes | None:
    """The content of ``path`` at ``commit``, or None if it is not there."""
    proc = _git_bytes(repo, "cat-file", "blob", f"{commit}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def text_files_have_crlf(repo: Path, commit: str) -> bool:
    """True if a tracked text file of ``commit`` contains ``\\r\\n``.

    A file with a NUL byte is binary and does not count.
    """
    listing = _git_bytes(repo, "ls-tree", "-r", "-z", commit)
    if listing.returncode != 0:
        raise GitError(f"git ls-tree {commit} failed in {repo}: "
                       f"{listing.stderr.decode('utf-8', errors='replace').strip()}")
    blobs = []
    for entry in listing.stdout.split(b"\0"):
        fields = entry.partition(b"\t")[0].split()
        if len(fields) == 3 and fields[1] == b"blob":
            blobs.append(fields[2])
    if not blobs:
        return False
    batch = _git_bytes(repo, "cat-file", "--batch",
                       stdin=b"\n".join(dict.fromkeys(blobs)) + b"\n").stdout
    position = 0
    while position < len(batch):
        end = batch.index(b"\n", position)
        header = batch[position:end].split()
        if len(header) < 3:  # "<id> missing"
            position = end + 1
            continue
        size = int(header[2])
        data = batch[end + 1:end + 1 + size]
        position = end + 1 + size + 1
        if b"\0" not in data and b"\r\n" in data:
            return True
    return False


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


def merge_commit_into(worktree: Path, commit: str, message: str) -> bool:
    """Merge ``commit`` into the branch checked out in ``worktree``.

    Returns False after a conflict; the worktree is then restored to its
    state before the merge.
    """
    proc = git(worktree, "merge", "-q", "--no-ff", "--no-verify", "-m", message, commit,
               check=False)
    if proc.returncode != 0:
        git(worktree, "merge", "--abort", check=False)
        git(worktree, "reset", "-q", "--hard", "HEAD")
        return False
    return True


def merge_keep_conflicts(worktree: Path, commit: str) -> list[str]:
    """Merge ``commit`` into the branch checked out in ``worktree``, without committing.

    Returns the files with conflicts, which keep their conflict markers
    (``<<<<<<<``, ``=======``, ``>>>>>>>``); an empty list means the merge was
    clean. Either way the merge stays open until :func:`commit_merge`.
    Raises ``GitError`` when git could not start the merge at all.
    """
    proc = git(worktree, "merge", "--no-ff", "--no-commit", "--no-verify", commit,
               check=False)
    output = git(worktree, "-c", "core.quotepath=off", "diff", "--name-only",
                 "--diff-filter=U", "-z").stdout
    conflicts = sorted(name for name in output.split("\0") if name)
    if proc.returncode != 0 and not conflicts:
        detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        raise GitError(f"git merge {commit} failed in {worktree}: {detail}")
    return conflicts


def commit_merge(worktree: Path, message: str) -> None:
    """Stage everything and conclude the merge that ``merge_keep_conflicts`` opened."""
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "--no-verify", "--allow-empty", "-m", message)


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


def source_state(path: Path) -> dict | None:
    """HEAD and dirtiness of the git checkout rooted at ``path``, or None.

    Returns None when ``path`` is not the top level of a git working tree
    (for example an installed package inside some other repository).
    """
    try:
        top = git(path, "rev-parse", "--show-toplevel", check=False)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != path.resolve():
            return None
        commit = git(path, "rev-parse", "HEAD", check=False)
        status = git(path, "status", "--porcelain", check=False)
    except GitError:
        return None
    if commit.returncode != 0 or status.returncode != 0:
        return None
    return {"head": commit.stdout.strip(), "dirty": bool(status.stdout.strip())}
