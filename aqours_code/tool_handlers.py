from .runtime_state import *
from .runtime import AgentRuntime
import fnmatch as _fnmatch
import os as _os
import re
from pathlib import Path

# ── Lead Worktree Tools ──

def run_create_worktree(name: str, task_id: str = "") -> str:
    return create_worktree(name, task_id)

def run_remove_worktree(name: str, discard_changes: bool = False) -> str:
    return remove_worktree(name, discard_changes)

def run_keep_worktree(name: str) -> str:
    return keep_worktree(name)

def run_integrate_worktree(
    name: str,
    cleanup: bool = True,
    runtime: AgentRuntime | None = None,
) -> str:
    return integrate_worktree(name, cleanup)

def run_delegate_agent(
    role: str,
    prompt: str,
    name: str = "",
    runtime: AgentRuntime | None = None,
) -> str:
    return delegate_agent(role, prompt, name, runtime)


# ── Basic tool handlers ──

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks)


def run_get_task(task_id: str) -> str:
    try:
        return get_task_json(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_claim_task(task_id: str) -> str:
    try:
        return claim_task(task_id, owner="agent")
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_spawn_teammate(
    name: str,
    role: str,
    prompt: str,
    runtime: AgentRuntime | None = None,
) -> str:
    return spawn_teammate_thread(name, role, prompt, runtime=runtime)

def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"Sent to {to}"

def run_check_inbox() -> str:
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        meta = m.get("metadata", {})
        req_id = meta.get("request_id", "")
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return "\n".join(lines)

def run_connect_mcp(name: str) -> str:
    return connect_mcp(name)


# ── Read-only search ──

_GREP_DEFAULT_RESULTS = 100
_GREP_MAX_RESULTS = 300
_GREP_LINE_CHARS = 200
_GREP_OUTPUT_CHARS = 8000
_GREP_MAX_FILE_BYTES = 2 * 1024 * 1024
_GREP_BINARY_SNIFF_BYTES = 8192
_GREP_SKIPPED_DIRS = frozenset({".git"})


def _grep_files(target: Path, base: Path):
    """Yield the files under ``target`` (or ``target`` itself) inside ``base``, in order."""
    if target.is_file():
        yield target
        return
    for root, dirs, files in _os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in _GREP_SKIPPED_DIRS)
        for name in sorted(files):
            if name in _GREP_SKIPPED_DIRS:  # a worktree's .git file
                continue
            path = Path(root) / name
            try:
                if path.resolve().is_relative_to(base):
                    yield path
            except OSError:
                continue


def _grep_text(path: Path) -> str | None:
    """The text of a searchable file; None for a binary or too large file."""
    try:
        if path.stat().st_size > _GREP_MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:_GREP_BINARY_SNIFF_BYTES]:
        return None
    return raw.decode(errors="replace")


def _grep_name_matches(path: Path, base: Path, file_glob: str) -> bool:
    if not file_glob:
        return True
    if "/" in file_glob:
        return _fnmatch.fnmatch(path.relative_to(base).as_posix(), file_glob)
    return _fnmatch.fnmatch(path.name, file_glob)


def run_grep(pattern: str, path: str = "", glob: str = "",
             ignore_case: bool = False, max_results: int = _GREP_DEFAULT_RESULTS,
             cwd: Path = None, runtime: AgentRuntime | None = None) -> str:
    """Search file contents with a Python regular expression, read-only.

    Pure Python (``re`` and a directory walk), no external command. Skips
    ``.git``, binary files and files over 2 MB; never leaves the workspace.
    Every problem is returned as an ``Error:`` line, never raised.
    """
    try:
        if not str(pattern or ""):
            return "Error: pattern must not be empty"
        try:
            limit = int(max_results if max_results is not None else _GREP_DEFAULT_RESULTS)
        except (TypeError, ValueError):
            return f"Error: max_results must be an integer, got {max_results!r}"
        limit = max(1, min(limit, _GREP_MAX_RESULTS))
        try:
            regex = re.compile(str(pattern), re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            return f"Error: invalid regular expression {str(pattern)!r}: {exc}"
        if cwd is not None:
            base = Path(cwd).resolve()
        elif runtime is not None:
            base = Path(runtime.paths.workdir).resolve()
        else:
            base = Path(WORKDIR).resolve()
        target = (base / str(path or ".")).resolve()
        if not target.is_relative_to(base):
            return f"Error: Path escapes workspace: {path}"
        if not target.exists():
            return f"Error: path not found: {path}"
        if target.is_file() and _grep_text(target) is None:
            return f"Error: {path} is binary or larger than 2 MB; not searched"
        file_glob = str(glob or "").replace("\\", "/")

        lines: list[str] = []
        size = 0
        total = 0
        stopped_by = ""
        for file_path in _grep_files(target, base):
            if not _grep_name_matches(file_path, base, file_glob):
                continue
            text = _grep_text(file_path)
            if text is None:
                continue
            relative = file_path.relative_to(base).as_posix()
            for number, line in enumerate(text.splitlines(), start=1):
                if not regex.search(line):
                    continue
                total += 1
                if stopped_by:
                    continue
                content = line.rstrip()
                if len(content) > _GREP_LINE_CHARS:
                    content = content[:_GREP_LINE_CHARS] + " ...(line truncated)"
                entry = f"{relative}:{number}: {content}"
                if len(lines) >= limit:
                    stopped_by = f"max_results={limit}"
                elif size + len(entry) + 1 > _GREP_OUTPUT_CHARS:
                    stopped_by = f"output limit of {_GREP_OUTPUT_CHARS} characters"
                else:
                    lines.append(entry)
                    size += len(entry) + 1
        if not total:
            return "no matches"
        if stopped_by:
            lines.append(f"... {total - len(lines)} more matches not shown ({stopped_by}); "
                         "narrow the pattern, path or glob")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 - a tool result, never an exception
        return f"Error: {type(exc).__name__}: {exc}"



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
