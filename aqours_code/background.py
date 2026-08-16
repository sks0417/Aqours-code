from .runtime_state import *

# ── Background Tasks ──

# Slow tools return a placeholder tool_result immediately. Their real output is
# later injected as a task_notification, so the main loop can keep moving.
_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, dict] = {}
background_lock = threading.Lock()

class BackgroundNotification(str):
    """String-compatible notification carrying fields needed by Trace."""

    def __new__(cls, text: str, *, task_id: str, status: str, command: str,
                summary: str, original_size: int, truncated: bool,
                externalized: bool = False, backing_path: str = "",
                original_estimated_tokens: int = 0, digest: str = ""):
        value = super().__new__(cls, text)
        value.task_id = task_id
        value.status = status
        value.command = command
        value.summary = summary
        value.original_size = original_size
        value.truncated = truncated
        value.externalized = externalized
        value.backing_path = backing_path
        value.original_estimated_tokens = original_estimated_tokens
        value.digest = digest
        return value


def _foreground_policy(_tool_name: str, _tool_input: dict) -> str | None:
    return None


def _explicit_policy(
    tool_name: str,
    tool_input: dict,
) -> str | None:
    if tool_name == "bash" and bool(tool_input.get("run_in_background")):
        return "explicit"
    return None


BACKGROUND_POLICY_ROUTERS = {
    "foreground": _foreground_policy,
    "explicit": _explicit_policy,
}


def has_background_policy_router(policy: str) -> bool:
    return policy in BACKGROUND_POLICY_ROUTERS


def background_reason(tool_name: str, tool_input: dict) -> str | None:
    try:
        policy = get_tool_spec(tool_name).background_policy
    except (KeyError, NameError):
        policy = "foreground"
    if not BACKGROUND_TASKS_ENABLED:
        return None
    router = BACKGROUND_POLICY_ROUTERS.get(policy)
    if router is None:
        return None
    return router(tool_name, tool_input)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    return background_reason(tool_name, tool_input) is not None


def start_background_task(
    block,
    handlers: dict,
    *,
    runtime=None,
    result_materializer=None,
) -> str:
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    command = block.input.get("command", block.name)

    task = {
        "tool_use_id": block.id,
        "command": command,
        "status": "running",
        "thread": None,
    }

    def worker():
        status = "completed"
        try:
            handler = handlers.get(block.name)
            result = call_tool_handler(
                handler,
                block.input,
                block.name,
                tool_use_id=block.id,
            )
            trigger_hooks("PostToolUse", block, result)
        except BaseException as exc:
            status = "failed"
            result = f"[Error] {type(exc).__name__}: {exc}"
        try:
            if result_materializer is not None:
                content, metadata = result_materializer(block, result, runtime)
            else:
                content = str(result)
                metadata = {
                    "externalized": False,
                    "backing_path": "",
                    "original_estimated_tokens": 0,
                    "original_chars": len(content),
                    "digest": "",
                }
        except BaseException as exc:
            # A failed artifact write must not strand the task in "running".
            # Surface the materialization failure through the normal completed
            # notification path so the lead can recover or rerun explicitly.
            status = "failed"
            content = f"[Error] {type(exc).__name__}: {exc}"
            metadata = {
                "externalized": False,
                "backing_path": "",
                "original_estimated_tokens": 0,
                "original_chars": len(content),
                "digest": "",
            }
        with background_lock:
            # Keep a direct reference to the task record. Runtime cleanup must
            # still wait for this worker before restoring the owning dict, but
            # this also prevents a late worker from indexing a replaced dict.
            task["status"] = status
            background_results[bg_id] = {
                "content": content,
                "metadata": metadata,
            }

    thread = threading.Thread(
        target=worker, name=f"aqours-code-background-{bg_id}", daemon=True)
    task["thread"] = thread
    with background_lock:
        background_tasks[bg_id] = task
    thread.start()
    print(f"  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    with background_lock:
        ready = [bg_id for bg_id, task in background_tasks.items()
                 if task["status"] in {"completed", "failed"}
                 and not task.get("thread").is_alive()]
    notifications = []
    for bg_id in ready:
        with background_lock:
            task = background_tasks.pop(bg_id)
            result = background_results.pop(bg_id)
        summary = str(result["content"])
        metadata = result["metadata"]
        externalized = bool(metadata.get("externalized"))
        text = (
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>{task['status']}</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        notifications.append(BackgroundNotification(
            text,
            task_id=bg_id,
            status=task["status"],
            command=str(task["command"]),
            summary=summary,
            original_size=int(metadata.get("original_chars") or len(summary)),
            truncated=externalized,
            externalized=externalized,
            backing_path=str(metadata.get("backing_path") or ""),
            original_estimated_tokens=int(
                metadata.get("original_estimated_tokens") or 0),
            digest=str(metadata.get("digest") or ""),
        ))
    return notifications


def wait_for_background_tasks(timeout: float | None = None) -> bool:
    """Wait up to timeout for active workers; return whether all stopped."""
    deadline = None if timeout is None else time.monotonic() + max(0, timeout)
    while True:
        with background_lock:
            threads = [task.get("thread") for task in background_tasks.values()]
        threads = [thread for thread in threads if thread is not None]
        threads = [thread for thread in threads if thread.is_alive()]
        if not threads:
            return True
        for thread in threads:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if remaining == 0:
                return False
            thread.join(remaining)
        if deadline is not None and time.monotonic() >= deadline:
            return False


def background_workers_alive() -> bool:
    with background_lock:
        return any(
            task.get("thread") is not None and task["thread"].is_alive()
            for task in background_tasks.values()
        )



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
