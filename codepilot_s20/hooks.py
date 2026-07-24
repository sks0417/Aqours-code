from .runtime_state import *
from .command_safety import looks_like_delete_command
from pathlib import Path
import re

# ── Hooks + Permission Pipeline ──

# Hooks are intentionally outside tool handlers. The loop can add permission,
# logging, and stop behavior without changing each individual tool.
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [],
         "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["> /etc/", "chmod 777"]


def _looks_like_delete_command(command: str) -> bool:
    return looks_like_delete_command(command)


def _looks_like_temp_cleanup(command: str) -> bool:
    lowered = command.lower()
    temp_markers = (
        "temp",
        "tmp",
        ".tmp",
        ".codepilot\\tmp",
        ".codepilot/tmp",
    )
    has_temp_target = any(marker in lowered for marker in temp_markers)
    has_read_only_work = any(token in lowered for token in (
        "dir ",
        "find ",
        "findstr",
        "sort",
        "type ",
        "glob",
    ))
    return has_temp_target and has_read_only_work


def recoverable_tool_rejection(message: str, guidance: str = "") -> dict:
    return {
        "kind": "tool_policy_rejection",
        "recoverable": True,
        "message": message,
        "guidance": guidance,
    }


def noninteractive_permission_denial(message: str) -> dict:
    return {
        "kind": "tool_policy_rejection",
        "recoverable": False,
        "message": f"Permission denied: {message}",
        "guidance": (
            "No interactive approver is available for this run. Continue only "
            "with operations that do not require approval."
        ),
    }


def _request_interactive_approval(prompt: str) -> bool:
    if APPROVAL_MODE != "interactive":
        return False
    return input(prompt).strip().lower() in ("y", "yes")


def _standard_policy(_block):
    return None


def _command_guard_policy(block):
    command = block.input.get("command", "")
    if _looks_like_delete_command(command):
        if _looks_like_temp_cleanup(command):
            return recoverable_tool_rejection(
                "Tool not run: bash delete commands are disabled.",
                ("Continue without deleting files. Avoid temp files for "
                 "read-only analysis; use glob/read_file or a read-only "
                 "cmd command instead. Do not retry a delete command."),
            )
        return "Permission denied: delete commands are disabled for bash"
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Permission denied: '{pattern}' is on the deny list"
    if any(token in command for token in DESTRUCTIVE):
        if APPROVAL_MODE != "interactive":
            return noninteractive_permission_denial(
                "destructive bash command requires interactive approval")
        print(f"\n\033[33m[permission] destructive command\033[0m")
        print(f"  {command}")
        if not _request_interactive_approval("  Allow? [y/N] "):
            return "Permission denied by user"
    return None


def _workspace_write_policy(block):
    path = block.input.get("path", "")
    try:
        safe_path(path)
    except Exception:
        return f"Permission denied: path escapes workspace: {path}"
    return None


def _destructive_confirmation_policy(block):
    if not bool(block.input.get("discard_changes")):
        return None
    if APPROVAL_MODE != "interactive":
        return noninteractive_permission_denial(
            "discarding Worktree changes requires interactive approval")
    name = str(block.input.get("name", ""))
    print(
        "\n\033[33m[permission] discard Worktree changes\033[0m"
        f"\n  {name}"
    )
    if not _request_interactive_approval("  Allow? [y/N] "):
        return "Permission denied by user"
    return None


def _workspace_integration_policy(block):
    name = str(block.input.get("name", "")).strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return "Permission denied: invalid Worktree name"
    root = Path(WORKTREES_DIR).resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        return "Permission denied: Worktree escapes managed root"
    return None


SAFETY_POLICY_VALIDATORS = {
    "standard": _standard_policy,
    "command_guard": _command_guard_policy,
    "workspace_write": _workspace_write_policy,
    "destructive_confirmation": _destructive_confirmation_policy,
    "workspace_integration": _workspace_integration_policy,
}


def has_safety_policy_validator(policy: str) -> bool:
    return policy in SAFETY_POLICY_VALIDATORS


def permission_hook(block):
    # The permission layer sees the raw tool_use before dispatch. It can deny,
    # ask the user, or allow execution to continue.
    try:
        safety_policy = get_tool_spec(block.name).safety_policy
    except (KeyError, NameError):
        safety_policy = "standard"
    validator = SAFETY_POLICY_VALIDATORS.get(safety_policy)
    if validator is None:
        return (
            "Permission denied: tool declares an unsupported safety policy "
            f"'{safety_policy}'"
        )
    rejected = validator(block)
    if rejected is not None:
        return rejected
    if block.name.startswith("mcp__") and "deploy" in block.name:
        if APPROVAL_MODE != "interactive":
            return noninteractive_permission_denial(
                f"{block.name} requires interactive approval")
        print(f"\n\033[33m[permission] MCP destructive-looking tool: {block.name}\033[0m")
        if not _request_interactive_approval("  Allow? [y/N] "):
            return "Permission denied by user"
    return None


def log_hook(block):
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] large output from {block.name}: "
              f"{len(str(output))} chars\033[0m")
    return None


def user_prompt_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: {WORKDIR}\033[0m")
    return None


def stop_hook(messages: list):
    tool_count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            tool_count += sum(1 for item in content
                              if isinstance(item, dict)
                              and item.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: {tool_count} tool result(s)\033[0m")
    return None


register_hook("UserPromptSubmit", user_prompt_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", stop_hook)



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
