from .runtime_state import *

# ── Background Tasks ──

# Slow tools return a placeholder tool_result immediately. Their real output is
# later injected as a task_notification, so the main loop can keep moving.
_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    command = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(keyword in command for keyword in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    if not BACKGROUND_TASKS_ENABLED:
        return False
    if tool_name != "bash":
        return False
    return bool(tool_input.get("run_in_background")) or is_slow_operation(tool_name, tool_input)


def start_background_task(block, handlers: dict) -> str:
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    command = block.input.get("command", block.name)

    def worker():
        handler = handlers.get(block.name)
        result = call_tool_handler(handler, block.input, block.name)
        trigger_hooks("PostToolUse", block, result)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = str(result)

    thread = threading.Thread(target=worker, daemon=True)
    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": command,
            "status": "running",
            "thread": thread,
        }
    thread.start()
    print(f"  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    with background_lock:
        ready = [bg_id for bg_id, task in background_tasks.items()
                 if task["status"] == "completed"]
    notifications = []
    for bg_id in ready:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
    return notifications


def wait_for_background_tasks(timeout: float | None = None) -> bool:
    """Wait up to timeout for active workers; return whether all stopped."""
    deadline = None if timeout is None else time.monotonic() + max(0, timeout)
    while True:
        with background_lock:
            threads = [task.get("thread") for task in background_tasks.values()
                       if task.get("status") == "running"]
        threads = [thread for thread in threads if thread is not None]
        if not threads:
            return True
        for thread in threads:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if remaining == 0:
                return False
            thread.join(remaining)
        if deadline is not None and time.monotonic() >= deadline:
            return False



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
