from .runtime_state import *
from .agent_profiles import get_agent_profile
import os as _os
import time as _time
from .command_executor import CaseTimeoutError as _CaseTimeoutError

# ── Subagent Tool ──

SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
]


SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read,
    "write_file": run_write, "edit_file": run_edit,
    "glob": run_glob,
}


def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        elif getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "\n".join(parts).strip()


def has_tool_use(content) -> bool:
    # Do not rely on stop_reason alone; the concrete tool_use block is the
    # continuation signal used by the loop.
    return any((block.get("type") if isinstance(block, dict)
                else getattr(block, "type", None)) == "tool_use"
               for block in content)


def _block_value(block, name: str, default=None):
    return block.get(name, default) if isinstance(block, dict) else getattr(block, name, default)


def _request_with_deadline(*, system: str, messages: list, tools: list,
                           purpose: str, role: str):
    remaining = None if CASE_DEADLINE is None else CASE_DEADLINE - _time.monotonic()
    if remaining is not None and remaining <= 0:
        raise _CaseTimeoutError("eval case deadline exceeded")
    old_timeout = _os.environ.get("MODEL_REQUEST_TIMEOUT")
    if remaining is not None:
        try:
            configured = float(old_timeout or "30")
        except (TypeError, ValueError):
            configured = 30.0
        _os.environ["MODEL_REQUEST_TIMEOUT"] = str(max(0.1, min(configured, remaining)))
    record_llm_request(
        model=MODEL, max_tokens=8000, message_count=len(messages),
        tool_count=len(tools), purpose=purpose, agent_role=role,
    )
    try:
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=tools, max_tokens=8000,
        )
        record_llm_response(response, purpose=purpose, agent_role=role)
        return response
    finally:
        if remaining is not None:
            if old_timeout is None:
                _os.environ.pop("MODEL_REQUEST_TIMEOUT", None)
            else:
                _os.environ["MODEL_REQUEST_TIMEOUT"] = old_timeout


def _role_handlers(cwd: Path) -> dict:
    pinned_executor = COMMAND_EXECUTOR
    return {
        "bash": lambda command: run_bash(
            command, cwd=cwd, executor=pinned_executor),
        "read_file": lambda path, limit=None, offset=0: run_read(
            path, limit=limit, offset=offset, cwd=cwd),
        "write_file": lambda path, content: run_write(path, content, cwd=cwd),
        "edit_file": lambda path, old_text, new_text: run_edit(
            path, old_text, new_text, cwd=cwd),
        "glob": lambda pattern: run_glob(pattern, cwd=cwd),
    }


def _parse_role_result(text: str, role: str) -> dict:
    raw = str(text or "").strip()
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S | re.I)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            value.setdefault("verdict", "blocked")
            value.setdefault("summary", "")
            return value
    fallback = "blocked" if role == "worker" else "inconclusive"
    return {"verdict": fallback, "summary": raw[:4000], "invalid_json": True}


def _safe_delegation_name(value: str, role: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-_")
    if not slug:
        slug = f"{role}-{int(_time.time())}-{random.randint(0, 9999):04d}"
    return slug[:64]


def run_role_agent(role: str, prompt: str, cwd: Path) -> dict:
    profile = get_agent_profile(role)
    if profile is None:
        return {"verdict": "blocked", "summary": f"unknown role: {role}"}
    tools = [tool for tool in SUB_TOOLS if tool["name"] in profile.tool_names]
    handlers = _role_handlers(cwd)
    policy = TOOL_POLICY if isinstance(TOOL_POLICY, dict) else {}
    prompt_runtime = resolve_prompt_runtime_context(policy, cwd)
    root_task = str(CURRENT_ROOT_TASK or "").strip() or "(not available)"
    system = (
        f"You are the {profile.name} role in a lead-managed coding task.\n"
        f"{profile.instructions}\n\n"
        f"{format_runtime_context_for_prompt(prompt_runtime)}\n"
        f"Assigned workspace: {cwd}\n"
        "The original root task is authoritative:\n"
        f"<root_task>\n{root_task}\n</root_task>"
    )
    messages = [{"role": "user", "content": prompt}]
    final_text = ""
    for _ in range(profile.max_rounds):
        response = _request_with_deadline(
            system=system, messages=messages, tools=tools,
            purpose="delegate_agent", role=profile.name,
        )
        messages.append({"role": "assistant", "content": response.content})
        text = extract_text(response.content)
        if text:
            final_text = text
        if not has_tool_use(response.content):
            break
        results = []
        for block in response.content:
            if _block_value(block, "type") != "tool_use":
                continue
            block_name = _block_value(block, "name", "")
            block_id = _block_value(block, "id", "")
            block_input = _block_value(block, "input", {}) or {}
            handler = handlers.get(block_name)
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                output = (tool_rejection_text(blocked)
                          if "tool_rejection_text" in globals() else str(blocked))
            else:
                output = call_tool_handler(handler, block_input, block_name)
                trigger_hooks("PostToolUse", block, output)
            record_event(
                "delegated_tool_use", agent_role=profile.name,
                tool=block_name, tool_use_id=block_id,
            )
            results.append({
                "type": "tool_result", "tool_use_id": block_id,
                "content": str(output),
            })
        messages.append({"role": "user", "content": results})
    return _parse_role_result(final_text, profile.name)


def delegate_agent(role: str, prompt: str, name: str = "",
                   task_id: str = "") -> str:
    """Run a bounded role with fresh context; workers are isolated by default."""
    normalized_role = str(role or "").strip().lower()
    profile = get_agent_profile(normalized_role)
    if profile is None:
        return json.dumps({
            "status": "error", "error": "role must be explorer, reviewer, or worker",
        })
    if not str(prompt or "").strip():
        return json.dumps({"status": "error", "error": "prompt cannot be empty"})

    record_event("delegation_start", agent_role=normalized_role, name=name)
    if not profile.uses_worktree:
        try:
            result = run_role_agent(normalized_role, prompt, WORKDIR)
        except Exception as exc:
            record_event(
                "delegation_finish", agent_role=normalized_role,
                verdict="blocked", status="error",
                error_type=type(exc).__name__, error=str(exc)[:1000],
            )
            return json.dumps({
                "status": "error", "role": normalized_role,
                "verdict": "blocked",
                "error": f"{type(exc).__name__}: {exc}"[:2000],
            })
        envelope = {
            "status": "completed", "role": normalized_role,
            "verdict": result.get("verdict", "inconclusive"),
            "result": result,
        }
        record_event(
            "delegation_finish", agent_role=normalized_role,
            verdict=envelope["verdict"], status=envelope["status"],
        )
        return json.dumps(envelope)

    worktree_name = _safe_delegation_name(name, normalized_role)
    if (WORKTREES_DIR / worktree_name).exists():
        return json.dumps({
            "status": "error", "role": normalized_role,
            "error": f"worktree already exists: {worktree_name}",
        })
    task = None
    if task_id:
        try:
            task = load_task(task_id)
        except FileNotFoundError:
            return json.dumps({"status": "error", "error": f"task not found: {task_id}"})
    else:
        task = create_task(
            f"Worker: {str(prompt).strip()[:80]}", str(prompt).strip())
    created = create_worktree(worktree_name, task.id)
    if not (WORKTREES_DIR / worktree_name).exists():
        return json.dumps({
            "status": "error", "role": normalized_role,
            "task_id": task.id, "error": created,
        })
    claimed = claim_task(task.id, owner=f"worker:{worktree_name}")
    if not claimed.startswith("Claimed"):
        return json.dumps({
            "status": "error", "role": normalized_role,
            "task_id": task.id, "worktree": worktree_name,
            "error": claimed,
        })

    try:
        result = run_role_agent(
            normalized_role, prompt, WORKTREES_DIR / worktree_name)
    except Exception as exc:
        record_event(
            "delegation_finish", agent_role=normalized_role,
            verdict="blocked", status="error", worktree=worktree_name,
            error_type=type(exc).__name__, error=str(exc)[:1000],
        )
        return json.dumps({
            "status": "error", "role": normalized_role,
            "verdict": "blocked", "task_id": task.id,
            "worktree": worktree_name,
            "error": f"{type(exc).__name__}: {exc}"[:2000],
            "recovery": "worktree retained with task in progress",
        })
    finalized = json.loads(finalize_worktree(
        worktree_name, f"worker({worktree_name}): {str(prompt).strip()[:120]}",
    ))
    if finalized.get("status") in {"changes_ready", "no_changes"}:
        complete_task(task.id)
    envelope = {
        "status": finalized.get("status", "error"),
        "role": normalized_role, "verdict": result.get("verdict", "blocked"),
        "task_id": task.id, "worktree": worktree_name,
        "commit": finalized.get("commit", ""),
        "changed_files": finalized.get("changed_files", []),
        "diff_stat": finalized.get("diff_stat", []),
        "result": result,
    }
    if finalized.get("error"):
        envelope["error"] = finalized["error"]
    record_event(
        "delegation_finish", agent_role=normalized_role,
        verdict=envelope["verdict"], status=envelope["status"],
        worktree=worktree_name, commit=envelope["commit"],
    )
    return json.dumps(envelope)


def spawn_subagent(description: str) -> str:
    messages = [{"role": "user", "content": description}]
    policy = TOOL_POLICY if isinstance(TOOL_POLICY, dict) else {}
    prompt_runtime = resolve_prompt_runtime_context(policy, WORKDIR)
    system = (
        f"You are a coding subagent at {prompt_runtime['workdir']}.\n\n"
        f"{format_runtime_context_for_prompt(prompt_runtime)}\n\n"
        "Complete the task, then return a concise final summary. "
        "Do not spawn more agents."
    )
    for _ in range(30):
        remaining = None if CASE_DEADLINE is None else CASE_DEADLINE - _time.monotonic()
        if remaining is not None and remaining <= 0:
            raise _CaseTimeoutError("eval case deadline exceeded")
        old_timeout = _os.environ.get("MODEL_REQUEST_TIMEOUT")
        if remaining is not None:
            try:
                configured = float(old_timeout or "30")
            except (TypeError, ValueError):
                configured = 30.0
            _os.environ["MODEL_REQUEST_TIMEOUT"] = str(max(0.1, min(configured, remaining)))
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=SUB_TOOLS, max_tokens=8000)
        finally:
            if remaining is not None:
                if old_timeout is None:
                    _os.environ.pop("MODEL_REQUEST_TIMEOUT", None)
                else:
                    _os.environ["MODEL_REQUEST_TIMEOUT"] = old_timeout
        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            break
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                output = str(blocked)
            else:
                handler = SUB_HANDLERS.get(block.name)
                output = call_tool_handler(handler, block.input, block.name)
                trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output)})
        messages.append({"role": "user", "content": results})
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text = extract_text(msg["content"])
            if text:
                return text
    return "Subagent finished without a text summary."



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
