from __future__ import annotations

from .runtime_state import *

from pathlib import Path as _Path
import shutil as _shutil
import os as _os
import time as _time
from .command_executor import CaseTimeoutError as _CaseTimeoutError

# ── Agent Loop ──

rounds_since_todo = 0
agent_lock = threading.Lock()


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_result":
                return ""
        elif getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "\n".join(parts)


def _latest_user_instruction(messages: list) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _message_text(message.get("content", ""))
        if text:
            return text
    return ""


def requires_initial_todo(messages: list) -> bool:
    text = _latest_user_instruction(messages).lower()
    if not text:
        return False
    action_markers = (
        "create", "write", "read", "glob", "list", "summarize", "edit", "run",
        "创建", "写入", "读取", "列出", "总结", "运行",
    )
    connector_markers = (
        " then ", " and ", "after", "然后", "再", "并", "接着",
    )
    multi_markers = (
        "multiple", "several", "files", "reports", "markdown", "directory",
        "多个", "文件", "目录",
    )
    action_count = sum(1 for marker in action_markers if marker in text)
    has_connector = any(marker in text for marker in connector_markers)
    has_multi_marker = any(marker in text for marker in multi_markers)
    has_number = re.search(r"\b([2-9]|[1-9]\d+)\b", text) is not None
    return action_count >= 3 and (has_connector or has_number) and (has_multi_marker or has_number)


def _context_stats(messages: list) -> dict:
    return {
        "message_count": len(messages),
        "estimated_size": estimate_size(messages),
    }


def _run_context_stage(stage: str, messages: list, func) -> list:
    before = _context_stats(messages)
    next_messages = func(messages)
    after = _context_stats(next_messages)
    changed = (before != after)
    if changed:
        record_event(
            "context_compact",
            stage=stage,
            changed=True,
            before_messages=before["message_count"],
            after_messages=after["message_count"],
            before_size=before["estimated_size"],
            after_size=after["estimated_size"],
        )
    return next_messages


def prepare_context(messages: list) -> list:
    # Every LLM turn enters through the same context budget pipeline.
    messages[:] = _run_context_stage("tool_result_budget", messages, tool_result_budget)
    messages[:] = _run_context_stage("snip_compact", messages, snip_compact)
    messages[:] = _run_context_stage("micro_compact", messages, micro_compact)
    if estimate_size(messages) > CONTEXT_LIMIT:
        before = _context_stats(messages)
        messages[:] = compact_history(messages)
        after = _context_stats(messages)
        record_event(
            "context_compact",
            stage="compact_history",
            changed=True,
            before_messages=before["message_count"],
            after_messages=after["message_count"],
            before_size=before["estimated_size"],
            after_size=after["estimated_size"],
        )
    return messages


def build_user_content(results: list[dict]) -> list[dict]:
    # Tool results and completed background notifications are both returned to
    # the model as user-side content, matching the tool_result feedback loop.
    content = list(results)
    for note in collect_background_results():
        content.append({"type": "text", "text": note})
    return content


def inject_background_notifications(messages: list):
    notes = collect_background_results()
    if notes:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})


def is_permission_denied_output(output) -> bool:
    return str(output).lower().startswith("permission denied")


def is_recoverable_tool_rejection(output) -> bool:
    return (isinstance(output, dict)
            and output.get("kind") == "tool_policy_rejection"
            and output.get("recoverable") is True)


def tool_rejection_text(output) -> str:
    if not (isinstance(output, dict)
            and output.get("kind") == "tool_policy_rejection"):
        return str(output)
    guidance = output.get("guidance", "")
    text = output.get("message", "Tool not run by policy.")
    if guidance:
        text += f"\nGuidance: {guidance}"
    return text


def todo_required_message() -> str:
    return ("Tool not run: this multi-step task needs an initial todo list. "
            "Call todo_write first with a short plan, then continue with the requested tools.")


def stop_after_permission_denied(messages: list, reason: str):
    if messages and messages[-1].get("role") == "assistant":
        messages[-1]["content"] = [{"type": "text", "text": reason}]
    else:
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": reason}
        ]})
    record_hook("Stop")
    trigger_hooks("Stop", messages)


def scheduled_prompt_text(job) -> str:
    label = "Scheduled Once" if getattr(job, "kind", "") == "once" else "Scheduled"
    return f"[{label}] {job.prompt}"


def call_llm(messages: list, context: dict, tools: list,
             state: RecoveryState, max_tokens: int):
    remaining = _remaining_case_time()
    if remaining is not None and remaining <= 0:
        raise _CaseTimeoutError("eval case deadline exceeded")
    system = assemble_system_prompt(context)
    record_llm_request(model=state.current_model, max_tokens=max_tokens,
                       message_count=len(messages), tool_count=len(tools))
    old_timeout = _os.environ.get("MODEL_REQUEST_TIMEOUT")
    if remaining is not None:
        try:
            configured = float(old_timeout or "30")
        except (TypeError, ValueError):
            configured = 30.0
        _os.environ["MODEL_REQUEST_TIMEOUT"] = str(max(0.1, min(configured, remaining)))
    try:
        return with_retry(
            lambda: client.messages.create(
                model=state.current_model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens),
            state)
    finally:
        if remaining is not None:
            if old_timeout is None:
                _os.environ.pop("MODEL_REQUEST_TIMEOUT", None)
            else:
                _os.environ["MODEL_REQUEST_TIMEOUT"] = old_timeout


def _remaining_case_time() -> float | None:
    if CASE_DEADLINE is None:
        return None
    return CASE_DEADLINE - _time.monotonic()


def _check_case_deadline():
    remaining = _remaining_case_time()
    if remaining is not None and remaining <= 0:
        raise _CaseTimeoutError("eval case deadline exceeded")


def agent_loop(messages: list, context: dict):
    global rounds_since_todo
    from . import bootstrap
    bootstrap()
    tools, handlers = assemble_tool_pool()
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS
    todo_required = requires_initial_todo(messages)
    todo_started = False

    while True:
        _check_case_deadline()
        # One cycle: inject scheduled/background work, prepare context, call
        # the model, execute tool_use blocks, append tool_results, repeat.
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user",
                             "content": scheduled_prompt_text(job)})
            prefix = "once inject" if getattr(job, "kind", "") == "once" else "cron inject"
            print(f"  \033[35m[{prefix}] {job.prompt[:60]}\033[0m")

        inject_background_notifications(messages)

        if rounds_since_todo >= 3:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        prepare_context(messages)
        context = update_context(context, messages)
        tools, handlers = assemble_tool_pool()

        try:
            response = call_llm(messages, context, tools, state, max_tokens)
            record_llm_response(response)
        except Exception as e:
            if isinstance(e, _CaseTimeoutError):
                raise
            record_error(e)
            if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                messages[:] = reactive_compact(messages)
                state.has_attempted_reactive_compact = True
                continue
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                continue
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            return

        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            if background_workers_alive() and CASE_DEADLINE is not None:
                remaining = _remaining_case_time()
                if not wait_for_background_tasks(remaining):
                    raise _CaseTimeoutError(
                        "eval case deadline exceeded while waiting for background tasks")
            # A worker may finish while the model is producing its final text,
            # so collect completed notifications even when no thread is alive
            # by the time this branch is reached.
            notes = collect_background_results()
            if notes:
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": note} for note in notes]})
                continue
            # Interactive CLI runs have no case deadline. Preserve real
            # background semantics: finish this turn immediately and inject
            # the task notification on a later user turn when it is ready.
            if background_workers_alive() and CASE_DEADLINE is None:
                record_hook("Stop")
                trigger_hooks("Stop", messages)
                finish_run(extract_text(response.content))
                return
            remaining = _remaining_case_time()
            notification_wait = 2.0 if remaining is None else max(0, min(2.0, remaining))
            if wait_for_imminent_once(notification_wait):
                continue
            record_hook("Stop")
            trigger_hooks("Stop", messages)
            finish_run(extract_text(response.content))
            return

        results = []
        compacted_now = False
        for block in response.content:
            _check_case_deadline()
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            record_tool_use(block)

            if todo_required and not todo_started and block.name not in ("todo_write", "compact"):
                output = todo_required_message()
                record_event("todo_gate", tool=block.name,
                             tool_use_id=block.id, input=block.input,
                             reason=output)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue

            if block.name == "compact":
                messages[:] = compact_history(messages)
                messages.append({"role": "user",
                                 "content": "[Compacted. Continue with summarized context.]"})
                record_tool_result(block.id, block.name,
                                   "[Compacted. Continue with summarized context.]")
                compacted_now = True
                break

            record_hook("PreToolUse", tool=block.name, stage="before")
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                blocked_text = tool_rejection_text(blocked)
                record_hook("PreToolUse", tool=block.name,
                            tool_use_id=block.id, input=block.input,
                            decision="blocked", reason=blocked_text,
                            recoverable=is_recoverable_tool_rejection(blocked))
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": blocked_text})
                record_tool_result(block.id, block.name, blocked_text)
                if is_recoverable_tool_rejection(blocked):
                    continue
                if is_permission_denied_output(blocked_text):
                    stop_after_permission_denied(messages, blocked_text)
                    finish_run(blocked_text)
                    return
                continue
            record_hook("PreToolUse", tool=block.name, decision="allowed")

            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block, handlers)
                output = (f"[Background task {bg_id} started] "
                          "Result will arrive as a task_notification. Do not "
                          "rerun the same command, poll with check_inbox, or "
                          "launch a task/subagent just to wait; continue "
                          "independent work or finish your turn.")
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue

            handler = handlers.get(block.name)
            output = call_tool_handler(handler, block.input, block.name)
            trigger_hooks("PostToolUse", block, output)
            record_hook("PostToolUse", tool=block.name)
            print(str(output)[:300])

            if block.name == "todo_write":
                rounds_since_todo = 0
                todo_started = True
            else:
                rounds_since_todo += 1

            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
            record_tool_result(block.id, block.name, output)
            if is_permission_denied_output(output):
                stop_after_permission_denied(messages, str(output))
                finish_run(str(output))
                return

        if compacted_now:
            continue

        messages.append({"role": "user", "content": build_user_content(results)})


def print_turn_assistants(messages: list, turn_start: int):
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if block_type(block) == "text":
                terminal_print(block["text"] if isinstance(block, dict) else block.text)


def cron_autorun_loop(history: list, context: dict):
    while True:
        time.sleep(1)
        fired = consume_cron_queue()
        if not fired:
            continue
        with agent_lock:
            turn_start = len(history)
            for job in fired:
                history.append({"role": "user",
                                "content": scheduled_prompt_text(job)})
                prefix = "once auto" if getattr(job, "kind", "") == "once" else "cron auto"
                terminal_print(
                    f"  \033[35m[{prefix}] {job.prompt[:60]}\033[0m")
            scheduled_prompt = "\n".join(
                scheduled_prompt_text(job) for job in fired)
            start_run(scheduled_prompt, workdir=WORKDIR,
                      model_provider=MODEL_PROVIDER, model=MODEL)
            try:
                agent_loop(history, context)
                context.update(update_context(context, history))
                print_turn_assistants(history, turn_start)
                final_text = ""
                for msg in reversed(history[turn_start:]):
                    if msg.get("role") == "assistant":
                        final_text = extract_text(msg.get("content", ""))
                        break
                finish_run(final_text)
            except Exception as e:
                record_error(e)
                finish_run(f"[Error] {type(e).__name__}: {e}")
                raise


def _set_runtime_value(name: str, value):
    from . import runtime_state as _state

    setattr(_state, name, value)
    for module in getattr(_state, "_REGISTERED_MODULES", []):
        if hasattr(module, name):
            setattr(module, name, value)
    if name == "WORKDIR":
        for module in getattr(_state, "_REGISTERED_MODULES", []):
            prompt_sections = getattr(module, "PROMPT_SECTIONS", None)
            if isinstance(prompt_sections, dict) and "workspace" in prompt_sections:
                prompt_sections["workspace"] = f"Working directory: {value}"


def _runtime_value(name: str):
    from . import runtime_state as _state

    return getattr(_state, name)


_WORKDIR_DERIVED_PATHS = {
    "SKILLS_DIR": ("skills",),
    "TRANSCRIPT_DIR": (".transcripts",),
    "TOOL_RESULTS_DIR": (".task_outputs", "tool-results"),
    "MEMORY_DIR": (".memory",),
    "MEMORY_INDEX": (".memory", "MEMORY.md"),
    "MAILBOX_DIR": (".mailboxes",),
    "TASKS_DIR": (".tasks",),
    "WORKTREES_DIR": (".worktrees",),
    "DURABLE_PATH": (".scheduled_tasks.json",),
    "ONCE_DURABLE_PATH": (".scheduled_once_tasks.json",),
}

_ISOLATED_RUNTIME_COLLECTIONS = (
    "mcp_clients", "active_teammates", "teammate_threads",
    "teammate_stop_events", "pending_requests", "scheduled_jobs",
    "scheduled_once_jobs", "CRON_LAST_FIRED", "background_tasks",
    "background_results",
)
_ISOLATED_RUNTIME_LISTS = ("cron_queue", "CURRENT_TODOS")


def _isolate_runtime_collections() -> dict:
    snapshots = {}
    for name in _ISOLATED_RUNTIME_COLLECTIONS:
        value = _runtime_value(name)
        snapshots[name] = dict(value)
        value.clear()
    for name in _ISOLATED_RUNTIME_LISTS:
        value = _runtime_value(name)
        snapshots[name] = list(value)
        value.clear()
    return snapshots


def _restore_runtime_collections(snapshots: dict):
    for name, snapshot in snapshots.items():
        value = _runtime_value(name)
        value.clear()
        if isinstance(value, dict):
            value.update(snapshot)
        else:
            value.extend(snapshot)


def _set_runtime_workdir(workdir, runtime_root=None):
    _set_runtime_value("WORKDIR", workdir)
    state_root = _Path(runtime_root).resolve() if runtime_root else workdir
    for name, parts in _WORKDIR_DERIVED_PATHS.items():
        _set_runtime_value(name, state_root.joinpath(*parts))


def _copy_trace_file(source, target):
    if not source or not target:
        return
    source_path = _Path(source)
    target_path = _Path(target)
    if source_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(source_path, target_path)


def run_agent_task(task: str, workdir: str, trace_path: str | None = None,
                   *, model_client=None, model_provider: str | None = None,
                   model: str | None = None, command_executor=None,
                   tool_policy: dict | None = None,
                   case_deadline: float | None = None,
                   cleanup_grace: float = 2.0,
                   trace_storage_root: str | None = None,
                   runtime_root: str | None = None,
                   manage_lifecycle: bool = False,
                   approval_mode: str | None = None) -> dict:
    """Run one non-interactive agent task using the existing loop and trace."""
    global rounds_since_todo
    from . import bootstrap
    bootstrap()

    workdir_path = _Path(workdir).resolve()
    workdir_path.mkdir(parents=True, exist_ok=True)
    state_names = [
        "WORKDIR", "client", "MODEL_PROVIDER", "MODEL", "PRIMARY_MODEL",
        "COMMAND_EXECUTOR", "TOOL_POLICY", "CASE_DEADLINE",
        "BACKGROUND_TASKS_ENABLED", "APPROVAL_MODE", *_WORKDIR_DERIVED_PATHS,
    ]
    old_state = {name: _runtime_value(name) for name in state_names}
    run = None
    final_text = ""
    rounds_since_todo = 0
    cleanup_errors = []
    collection_snapshots = None
    try:
        # The outer try begins before the first runtime mutation.
        _set_runtime_workdir(workdir_path, runtime_root)
        if model_client is not None:
            _set_runtime_value("client", model_client)
        if command_executor is not None:
            _set_runtime_value("COMMAND_EXECUTOR", command_executor)
        _set_runtime_value("TOOL_POLICY", tool_policy)
        _set_runtime_value("CASE_DEADLINE", case_deadline)
        if approval_mode is not None:
            if approval_mode not in {"interactive", "non_interactive"}:
                raise ValueError(f"unsupported approval mode: {approval_mode}")
            _set_runtime_value("APPROVAL_MODE", approval_mode)
        if manage_lifecycle:
            collection_snapshots = _isolate_runtime_collections()
        if isinstance(tool_policy, dict) and "background_tasks" in tool_policy:
            _set_runtime_value("BACKGROUND_TASKS_ENABLED", bool(tool_policy["background_tasks"]))

        provider_name = model_provider or _runtime_value("MODEL_PROVIDER")
        model_name = model or _runtime_value("MODEL")
        _set_runtime_value("MODEL_PROVIDER", provider_name)
        _set_runtime_value("MODEL", model_name)
        _set_runtime_value("PRIMARY_MODEL", model_name)
        run = start_run(task, workdir=workdir_path,
                        model_provider=provider_name, model=model_name,
                        storage_root=(_Path(trace_storage_root).resolve()
                                      if trace_storage_root else None))
        record_hook("UserPromptSubmit", input=task)
        trigger_hooks("UserPromptSubmit", task)
        if tool_policy:
            record_event("tool_policy", **tool_policy)

        messages = [{"role": "user", "content": task}]
        context = update_context({}, [])
        if manage_lifecycle:
            start_scheduler(load_durable=True)
        if command_executor is not None:
            command_executor.start()
        with agent_lock:
            agent_loop(messages, context)
            update_context(context, messages)
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                final_text = _message_text(msg.get("content", ""))
                break
        if get_current_run():
            finish_run(final_text)
    except Exception as exc:
        try:
            record_error(exc)
        except Exception:
            pass
        final_text = f"[Error] {type(exc).__name__}: {exc}"
        try:
            finish_run(final_text)
        except Exception:
            pass
        raise
    finally:
        active_exception = _sys.exc_info()[0] is not None
        cleanup_deadline = _time.monotonic() + max(0, cleanup_grace)

        def cleanup_remaining() -> float:
            return max(0, cleanup_deadline - _time.monotonic())

        def cleanup_step(func):
            try:
                func()
            except BaseException as cleanup_exc:
                cleanup_errors.append(cleanup_exc)

        def cleanup_status(func, failure_message: str) -> bool:
            try:
                stopped = bool(func())
            except BaseException as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
                return False
            if not stopped:
                cleanup_errors.append(RuntimeError(failure_message))
            return stopped

        lifecycle_stopped = True
        if manage_lifecycle:
            scheduler_stopped = cleanup_status(
                lambda: stop_scheduler(cleanup_remaining()),
                "scheduler thread did not stop",
            )
            teammates_stopped = cleanup_status(
                lambda: stop_all_teammates(cleanup_remaining()),
                "teammate threads did not stop",
            )
            lifecycle_stopped = scheduler_stopped and teammates_stopped
        # Stop the executor so in-flight command calls unblock, then give
        # background workers only a small bounded grace period.
        if command_executor is not None:
            cleanup_step(command_executor.stop)
        background_stopped = cleanup_status(
            lambda: wait_for_background_tasks(cleanup_remaining()),
            "background worker threads did not stop",
        )
        if run is not None:
            cleanup_step(lambda: _copy_trace_file(run.trace_path, trace_path))
        # Restoring these dicts while an owned worker still references them is
        # a race. A one-shot eval process will fail closed instead.
        if collection_snapshots is not None and lifecycle_stopped and background_stopped:
            cleanup_step(lambda: _restore_runtime_collections(collection_snapshots))
        for name in reversed(state_names):
            cleanup_step(lambda name=name: _set_runtime_value(name, old_state[name]))
        if cleanup_errors and not active_exception:
            raise cleanup_errors[0]

    return {
        "run_id": run.run_id,
        "run_dir": str(run.run_dir),
        "trace_path": str(_Path(trace_path).resolve()) if trace_path else str(run.trace_path),
        "timeline_path": str(run.timeline_path),
        "final_path": str(run.final_path),
        "final_answer": final_text,
        "execution": (command_executor or old_state["COMMAND_EXECUTOR"]).execution_metadata(),
    }



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
