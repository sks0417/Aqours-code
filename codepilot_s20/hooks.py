from .runtime_state import *

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
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


DELETE_COMMANDS = (
    "remove-item", "rmdir", "rd ", "del ", "erase ", "rm ", "unlink",
)


def _looks_like_delete_command(command: str) -> bool:
    lowered = f" {command.lower()} "
    return any(token in lowered for token in DELETE_COMMANDS)


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


def permission_hook(block):
    # The permission layer sees the raw tool_use before dispatch. It can deny,
    # ask the user, or allow execution to continue.
    if block.name == "bash":
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
            print(f"\n\033[33m[permission] destructive command\033[0m")
            print(f"  {command}")
            choice = input("  Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        try:
            safe_path(path)
        except Exception:
            return f"Permission denied: path escapes workspace: {path}"
    if block.name.startswith("mcp__") and "deploy" in block.name:
        print(f"\n\033[33m[permission] MCP destructive-looking tool: {block.name}\033[0m")
        choice = input("  Allow? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
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
