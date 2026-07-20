from .runtime_state import *
from .command_executor import CaseTimeoutError

# ── Basic Tools ──

def safe_path(p: str, cwd: Path = None) -> Path:
    # File tools stay inside the workspace or teammate worktree. Bash remains
    # powerful on purpose and is controlled by the permission hook instead.
    base = cwd or WORKDIR
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, cwd: Path = None,
             run_in_background: bool = False, timeout: float = 120,
             executor=None) -> str:
    # run_in_background is consumed by the dispatcher; direct execution ignores it.
    lowered = f" {command.lower()} "
    delete_commands = ("remove-item", "rmdir", "rd ", "del ", "erase ", "rm ", "unlink")
    if any(token in lowered for token in delete_commands):
        return "Permission denied: delete commands are disabled for bash"
    try:
        effective_timeout = float(timeout)
        if CASE_DEADLINE is not None:
            remaining = CASE_DEADLINE - time.monotonic()
            if remaining <= 0:
                raise CaseTimeoutError("eval case deadline exceeded")
            effective_timeout = min(effective_timeout, remaining)
        result = (executor or COMMAND_EXECUTOR).execute(
            command, cwd or WORKDIR, effective_timeout)
        out = (result["stdout"] + result["stderr"]).strip()
        if result["timed_out"]:
            return f"Error: Timeout ({timeout:g}s)" + (f"\n{out[:50000]}" if out else "")
        return out[:50000] if out else "(no output)"
    except CaseTimeoutError:
        raise
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def run_read(path: str, limit: int | None = None,
             offset: int = 0, cwd: Path = None) -> str:
    try:
        lines = safe_path(path, cwd).read_text().splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path = None) -> str:
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str,
             cwd: Path = None) -> str:
    try:
        fp = safe_path(path, cwd)
        text = fp.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        fp.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str, cwd: Path = None) -> str:
    import glob as g
    try:
        base = cwd or WORKDIR
        results = []
        for match in sorted(g.glob(pattern, root_dir=base, recursive=True)):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def call_tool_handler(handler, args: dict, name: str) -> str:
    if not handler:
        return f"Unknown: {name}"
    try:
        return handler(**(args or {}))
    except TypeError as e:
        return f"Error: {e}"


_MAX_TODO_ITEMS = 20
_MAX_ACCEPTANCE_ITEMS = 12
_MAX_TODO_TEXT = 500


def _normalize_todos(todos):
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
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in todo or "status" not in todo:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        content = str(todo["content"]).strip()
        if not content:
            return None, f"Error: todos[{i}] content must not be empty"
        if len(content) > _MAX_TODO_TEXT:
            return None, (
                f"Error: todos[{i}] content exceeds {_MAX_TODO_TEXT} characters")
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
        kind = str(todo.get("kind", "plan")).strip().lower()
        if kind not in ("plan", "acceptance"):
            return None, f"Error: todos[{i}] has invalid kind '{kind}'"
        evidence = str(todo.get("evidence", "")).strip()
        if len(evidence) > _MAX_TODO_TEXT:
            return None, (
                f"Error: todos[{i}] evidence exceeds {_MAX_TODO_TEXT} characters")
        if kind == "acceptance" and todo["status"] == "completed" and not evidence:
            return None, (
                f"Error: todos[{i}] completed acceptance item requires evidence")
        item = {
            "content": content,
            "status": todo["status"],
            "kind": kind,
        }
        if evidence:
            item["evidence"] = evidence
        normalized.append(item)
    acceptance_count = sum(
        1 for todo in normalized if todo["kind"] == "acceptance")
    if acceptance_count > _MAX_ACCEPTANCE_ITEMS:
        return None, (
            "Error: todos may contain at most "
            f"{_MAX_ACCEPTANCE_ITEMS} acceptance items")
    return normalized, None

def run_todo_write(todos: list) -> str:
    todos, error = _normalize_todos(todos)
    if error:
        return error
    # Mutate the shared runtime list instead of rebinding this module's copy;
    # Agent finalization and prompt assembly read the same live state.
    CURRENT_TODOS[:] = todos
    acceptance = [todo for todo in CURRENT_TODOS
                  if todo.get("kind") == "acceptance"]
    unverified = [todo for todo in acceptance
                  if todo.get("status") != "completed"]
    print(f"  \033[33m[todo] updated {len(CURRENT_TODOS)} item(s)\033[0m")
    detail = ""
    if acceptance:
        detail = (f" ({len(acceptance)} acceptance, "
                  f"{len(unverified)} unverified)")
    return f"Updated {len(CURRENT_TODOS)} todos{detail}"



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
