from .runtime_state import *
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
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text").strip()


def has_tool_use(content) -> bool:
    # Do not rely on stop_reason alone; the concrete tool_use block is the
    # continuation signal used by the loop.
    return any(getattr(block, "type", None) == "tool_use"
               for block in content)


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
