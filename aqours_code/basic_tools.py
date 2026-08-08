from .runtime_state import *
from .command_executor import CaseTimeoutError
from .command_safety import looks_like_delete_command
from .runtime import AgentRuntime
import hashlib
import re
from pathlib import PurePosixPath

# ── Basic Tools ──

def _runtime_workdir(
    runtime: AgentRuntime | None = None,
    cwd: Path | None = None,
) -> Path:
    if cwd is not None:
        return Path(cwd).resolve()
    if runtime is not None:
        return runtime.paths.workdir
    return Path(WORKDIR).resolve()


def _runtime_todos(runtime: AgentRuntime | None = None) -> list[dict]:
    return runtime.state.todos if runtime is not None else CURRENT_TODOS


def _normalize_observation_path(path: str) -> str:
    """Use one stable path spelling for repeated-read trace analysis."""
    value = str(path or "").strip().replace("\\", "/")
    while "//" in value:
        value = value.replace("//", "/")
    if value.startswith("./"):
        value = value[2:]
    return str(PurePosixPath(value)) if value else ""


def safe_path(
    p: str,
    cwd: Path = None,
    runtime: AgentRuntime | None = None,
) -> Path:
    # File tools stay inside the workspace or teammate worktree. Bash remains
    # powerful on purpose and is controlled by the permission hook instead.
    base = _runtime_workdir(runtime, cwd)
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, cwd: Path = None,
             run_in_background: bool = False, timeout: float = 120,
             executor=None, runtime: AgentRuntime | None = None,
             _report_exit_code: bool = False) -> str:
    # run_in_background is consumed by the dispatcher; direct execution ignores it.
    if looks_like_delete_command(command):
        return "Permission denied: delete commands are disabled for bash"
    try:
        effective_timeout = float(timeout)
        deadline = (
            runtime.state.deadline if runtime is not None else CASE_DEADLINE
        )
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaseTimeoutError("eval case deadline exceeded")
            effective_timeout = min(effective_timeout, remaining)
        selected_executor = (
            executor
            or (runtime.services.command_executor if runtime is not None else None)
            or COMMAND_EXECUTOR
        )
        workdir = _runtime_workdir(runtime, cwd)
        result = selected_executor.execute(
            command, workdir, effective_timeout)
        out = (result["stdout"] + result["stderr"]).strip()
        if result["timed_out"]:
            return f"Error: Timeout ({timeout:g}s)" + (f"\n{out[:50000]}" if out else "")
        rendered = out[:50000] if out else "(no output)"
        if _report_exit_code:
            return f"[exit_code={int(result['exit_code'])}]\n{rendered}"
        return rendered
    except CaseTimeoutError:
        raise
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def run_read(path: str, limit: int | None = None,
             offset: int = 0, cwd: Path = None,
             runtime: AgentRuntime | None = None,
             _tool_use_id: str = "") -> str:
    try:
        file_path = safe_path(path, cwd, runtime)
        raw = file_path.read_bytes()
        lines = raw.decode(errors="replace").splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        if runtime is not None:
            end = len(lines) if limit is None else min(
                len(lines), offset + max(0, limit),
            )
            record_event(
                "read_observation",
                tool_use_id=str(_tool_use_id),
                path=_normalize_observation_path(path),
                digest=hashlib.sha256(raw).hexdigest(),
                offset=offset,
                limit=limit,
                range_start=offset,
                range_end=end,
                compact_generation=int(
                    runtime.state.metadata.get("compact_generation", 0)
                ),
            )
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path = None,
              runtime: AgentRuntime | None = None) -> str:
    try:
        fp = safe_path(path, cwd, runtime)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str,
             cwd: Path = None, runtime: AgentRuntime | None = None) -> str:
    try:
        fp = safe_path(path, cwd, runtime)
        text = fp.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        fp.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str, cwd: Path = None,
             runtime: AgentRuntime | None = None) -> str:
    import glob as g
    try:
        base = _runtime_workdir(runtime, cwd)
        results = []
        for match in sorted(g.glob(pattern, root_dir=base, recursive=True)):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def call_tool_handler(
    handler,
    args: dict,
    name: str,
    *,
    tool_use_id: str = "",
) -> str:
    if not handler:
        return f"Unknown: {name}"
    try:
        kwargs = dict(args or {})
        if name == "read_file" and tool_use_id:
            kwargs["_tool_use_id"] = str(tool_use_id)
        return handler(**kwargs)
    except TypeError as e:
        return f"Error: {e}"


_MAX_TODO_ITEMS = 32
_MAX_TODO_TEXT = 500
_MAX_TODO_ID = 100
_TODO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]*$")


def _next_todo_id(used_ids: set[str]) -> str:
    index = 1
    while f"todo:{index}" in used_ids:
        index += 1
    value = f"todo:{index}"
    used_ids.add(value)
    return value


def _normalize_todos(todos, runtime: AgentRuntime | None = None):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    if len(todos) > _MAX_TODO_ITEMS:
        return None, f"Error: todos may contain at most {_MAX_TODO_ITEMS} items"
    normalized = []
    current_todos = _runtime_todos(runtime)
    existing_by_id = {
        str(todo["id"]): todo for todo in current_todos if todo.get("id")
    }
    existing_by_content = {
        str(todo.get("content", "")): todo for todo in current_todos
        if todo.get("content")
    }
    used_ids = set(existing_by_id)
    submitted_ids: set[str] = set()
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "status" not in todo:
            return None, f"Error: todos[{i}] missing 'status'"
        todo_id = str(todo.get("id", "")).strip()
        existing = existing_by_id.get(todo_id) if todo_id else None
        if "content" not in todo and existing is None:
            return None, (
                f"Error: todos[{i}] requires 'content' for a new item or a "
                "known 'id' for an update")
        content = str(todo.get(
            "content",
            existing.get("content", "") if existing else "",
        )).strip()
        if not content:
            return None, f"Error: todos[{i}] content must not be empty"
        if len(content) > _MAX_TODO_TEXT:
            return None, (
                f"Error: todos[{i}] content exceeds {_MAX_TODO_TEXT} characters")
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
        item = {
            "content": content,
            "status": todo["status"],
        }
        if not todo_id:
            matched = existing_by_content.get(content)
            if matched and matched.get("id"):
                todo_id = str(matched["id"])
            else:
                todo_id = _next_todo_id(used_ids)
        if todo_id:
            if (len(todo_id) > _MAX_TODO_ID
                    or not _TODO_ID_PATTERN.fullmatch(todo_id)):
                return None, f"Error: todos[{i}] has invalid id '{todo_id}'"
            if todo_id in submitted_ids:
                return None, f"Error: duplicate todo id '{todo_id}'"
            submitted_ids.add(todo_id)
            used_ids.add(todo_id)
            item["id"] = todo_id
        normalized.append(item)
    return normalized, None

def run_todo_write(
    todos: list,
    runtime: AgentRuntime | None = None,
) -> str:
    todos, error = _normalize_todos(todos, runtime)
    if error:
        return error
    # Mutate the shared runtime list instead of rebinding this module's copy;
    # Agent finalization and prompt assembly read the same live state.
    current_todos = _runtime_todos(runtime)
    current_todos[:] = todos
    print(f"  \033[33m[todo] updated {len(current_todos)} item(s)\033[0m")
    return f"Updated {len(current_todos)} todos"



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
