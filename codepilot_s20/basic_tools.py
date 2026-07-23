from .runtime_state import *
from .command_executor import CaseTimeoutError
from .command_safety import looks_like_delete_command
from .knowledge import content_digest, normalize_knowledge_path
from .runtime import AgentRuntime
import re

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


def _record_runtime_mutation(
    runtime: AgentRuntime | None,
    path: str,
    file_path: Path,
) -> None:
    if runtime is None:
        return
    digest = runtime.state.knowledge.files.get(normalize_knowledge_path(path))
    current_digest = content_digest(file_path.read_bytes())
    if digest is not None and digest.digest == current_digest:
        return
    runtime.state.knowledge.invalidate_file(
        path, digest=current_digest, modified=True,
    )


def run_bash(command: str, cwd: Path = None,
             run_in_background: bool = False, timeout: float = 120,
             executor=None, runtime: AgentRuntime | None = None) -> str:
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
        result = selected_executor.execute(
            command, _runtime_workdir(runtime, cwd), effective_timeout)
        out = (result["stdout"] + result["stderr"]).strip()
        if runtime is not None:
            runtime.state.knowledge.record_test(
                command,
                exit_code=result.get("exit_code"),
                timed_out=bool(result["timed_out"]),
                result=out or "(no output)",
            )
        if result["timed_out"]:
            return f"Error: Timeout ({timeout:g}s)" + (f"\n{out[:50000]}" if out else "")
        return out[:50000] if out else "(no output)"
    except CaseTimeoutError:
        raise
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def run_read(path: str, limit: int | None = None,
             offset: int = 0, cwd: Path = None,
             runtime: AgentRuntime | None = None) -> str:
    try:
        file_path = safe_path(path, cwd, runtime)
        raw = file_path.read_bytes()
        lines = raw.decode(errors="replace").splitlines()
        if runtime is not None:
            runtime.state.knowledge.observe_file(path, raw)
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
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
        _record_runtime_mutation(runtime, path, fp)
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
        _record_runtime_mutation(runtime, path, fp)
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
_MAX_TODO_ID = 100
_TODO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]*$")


def _next_acceptance_id(used_ids: set[str]) -> str:
    index = 1
    while f"accept:{index}" in used_ids:
        index += 1
    value = f"accept:{index}"
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
        content = str(
            existing.get("content", "") if existing else todo.get("content", "")
        ).strip()
        if not content:
            return None, f"Error: todos[{i}] content must not be empty"
        if len(content) > _MAX_TODO_TEXT:
            return None, (
                f"Error: todos[{i}] content exceeds {_MAX_TODO_TEXT} characters")
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
        kind = str(
            existing.get("kind", "plan") if existing
            else todo.get("kind", "plan")
        ).strip().lower()
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
        if not todo_id:
            matched = existing_by_content.get(content)
            if matched and matched.get("id"):
                todo_id = str(matched["id"])
            elif kind == "acceptance":
                todo_id = _next_acceptance_id(used_ids)
        if todo_id:
            if (len(todo_id) > _MAX_TODO_ID
                    or not _TODO_ID_PATTERN.fullmatch(todo_id)):
                return None, f"Error: todos[{i}] has invalid id '{todo_id}'"
            if todo_id in submitted_ids:
                return None, f"Error: duplicate todo id '{todo_id}'"
            submitted_ids.add(todo_id)
            used_ids.add(todo_id)
            item["id"] = todo_id
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
    if runtime is not None:
        runtime.state.knowledge.sync_acceptance(current_todos)
    acceptance = [todo for todo in current_todos
                  if todo.get("kind") == "acceptance"]
    unverified = [todo for todo in acceptance
                  if todo.get("status") != "completed"]
    print(f"  \033[33m[todo] updated {len(current_todos)} item(s)\033[0m")
    detail = ""
    if acceptance:
        detail = (f" ({len(acceptance)} acceptance, "
                  f"{len(unverified)} unverified)")
    return f"Updated {len(current_todos)} todos{detail}"



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
