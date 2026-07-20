from .runtime_state import *

import re as _re
import shlex as _shlex

# ── Background Tasks ──

# Slow tools return a placeholder tool_result immediately. Their real output is
# later injected as a task_notification, so the main loop can keep moving.
_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()

_BACKGROUND_SUMMARY_LIMIT = 4000
_BACKGROUND_SUMMARY_HEAD = 1200


class BackgroundNotification(str):
    """String-compatible notification carrying fields needed by Trace."""

    def __new__(cls, text: str, *, task_id: str, status: str, command: str,
                summary: str, original_size: int, truncated: bool):
        value = super().__new__(cls, text)
        value.task_id = task_id
        value.status = status
        value.command = command
        value.summary = summary
        value.original_size = original_size
        value.truncated = truncated
        return value


def _summarize_background_output(output: str) -> tuple[str, bool]:
    """Keep both command context and the completion summary in notifications."""
    if len(output) <= _BACKGROUND_SUMMARY_LIMIT:
        return output, False
    omitted = len(output) - _BACKGROUND_SUMMARY_LIMIT
    tail_size = _BACKGROUND_SUMMARY_LIMIT - _BACKGROUND_SUMMARY_HEAD
    return (
        output[:_BACKGROUND_SUMMARY_HEAD]
        + f"\n... ({omitted} characters omitted) ...\n"
        + output[-tail_size:]
    ), True


def _command_segments(command: str) -> list[list[str]]:
    """Tokenize executable shell segments without scanning arguments as verbs."""
    text = str(command or "").strip()
    if not text:
        return []
    # A heredoc body is data, not more shell commands. Inspect only the command
    # line that opens it so embedded words such as ``pytest`` do not classify a
    # quick script-writing command as a slow test run.
    if "<<" in text:
        text = text.splitlines()[0]
    raw_segments = _re.split(r"\s*(?:&&|\|\||;|\n)\s*", text)
    segments = []
    for segment in raw_segments:
        if not segment:
            continue
        try:
            words = _shlex.split(segment, posix=True)
        except ValueError:
            words = segment.split()
        if words:
            segments.append(words)
    return segments


def _strip_command_prefixes(words: list[str]) -> list[str]:
    tokens = list(words)
    while tokens and _re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    while tokens:
        executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if executable in {"env", "command", "sudo", "nohup"}:
            tokens.pop(0)
            while tokens and (tokens[0].startswith("-")
                              or _re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])):
                tokens.pop(0)
            continue
        if executable == "timeout" and len(tokens) >= 2:
            tokens.pop(0)
            while tokens and tokens[0].startswith("-"):
                tokens.pop(0)
            if tokens:
                tokens.pop(0)
            continue
        break
    return tokens


def _is_slow_command_segment(words: list[str]) -> bool:
    tokens = _strip_command_prefixes(words)
    if not tokens:
        return False
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    args = [str(value).lower() for value in tokens[1:]]

    if executable in {"sh", "bash", "zsh"}:
        for flag in ("-c", "-lc"):
            if flag in args:
                index = args.index(flag)
                if index + 1 < len(tokens[1:]):
                    nested = tokens[1:][index + 1]
                    return any(
                        _is_slow_command_segment(segment)
                        for segment in _command_segments(nested)
                    )
        return False
    if executable in {"pytest", "py.test"}:
        return True
    if executable.startswith(("python", "pypy")):
        return len(args) >= 2 and args[0] == "-m" and args[1] in {
            "pytest", "unittest", "pip",
        } and (args[1] != "pip" or "install" in args[2:])
    if executable in {"pip", "pip3"}:
        return "install" in args
    if executable in {"npm", "pnpm", "yarn"}:
        return any(arg in {"test", "install", "build"} for arg in args[:2])
    if executable == "cargo":
        return bool(args and args[0] in {"test", "build"})
    if executable == "docker":
        return bool(args and args[0] == "build")
    if executable in {"go", "mvn", "mvnw", "gradle", "gradlew"}:
        return any(arg in {"test", "build", "package", "install"}
                   for arg in args[:2])
    return executable == "make"


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    return any(
        _is_slow_command_segment(segment)
        for segment in _command_segments(tool_input.get("command", ""))
    )


def background_reason(tool_name: str, tool_input: dict) -> str | None:
    if not BACKGROUND_TASKS_ENABLED or tool_name != "bash":
        return None
    if bool(tool_input.get("run_in_background")):
        return "explicit"
    if is_slow_operation(tool_name, tool_input):
        return "slow_command"
    return None


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    return background_reason(tool_name, tool_input) is not None


def start_background_task(block, handlers: dict) -> str:
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
            result = call_tool_handler(handler, block.input, block.name)
            trigger_hooks("PostToolUse", block, result)
        except BaseException as exc:
            status = "failed"
            result = f"[Error] {type(exc).__name__}: {exc}"
        with background_lock:
            # Keep a direct reference to the task record. Runtime cleanup must
            # still wait for this worker before restoring the owning dict, but
            # this also prevents a late worker from indexing a replaced dict.
            task["status"] = status
            background_results[bg_id] = str(result)

    thread = threading.Thread(
        target=worker, name=f"codepilot-background-{bg_id}", daemon=True)
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
            output = background_results.pop(bg_id, "")
        summary, truncated = _summarize_background_output(output)
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
            original_size=len(output),
            truncated=truncated,
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
