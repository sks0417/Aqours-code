from __future__ import annotations

from .runtime_state import *

from pathlib import Path as _Path
import shutil as _shutil
import os as _os
import time as _time
from .command_executor import CaseTimeoutError as _CaseTimeoutError
from .agent_profiles import (
    assess_task_complexity,
    classify_delegation_intent,
    complex_delegation_briefing,
)
from .model_budget import (
    can_spend_optional_calls,
    finalization_reserve_active,
)
from .runtime import AgentRuntime

# ── Agent Loop ──

rounds_since_todo = 0
agent_lock = threading.Lock()
_MUTATING_FILE_TOOLS = {"write_file", "edit_file"}
_MAIN_MUTATION_TOOLS = _MUTATING_FILE_TOOLS | {"integrate_worktree"}


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


def requires_acceptance_todos(messages: list) -> bool:
    """Identify code tasks whose external requirements need a final audit."""
    text = _latest_user_instruction(messages).lower()
    if not text:
        return False
    change_markers = (
        "fix", "implement", "repair", "refactor", "debug", "modify code",
        "修复", "实现", "改代码", "重构", "调试",
    )
    requirement_markers = (
        "contract", "requirement", "readme", "test", "public api", "preserve",
        "bug", "consistency", "behavior", "acceptance",
        "契约", "要求", "测试", "接口", "保持", "错误", "一致性", "行为", "验收",
    )
    requirement_count = sum(
        1 for marker in requirement_markers if marker in text)
    return any(marker in text for marker in change_markers) and requirement_count >= 2


def _runtime_todos(runtime: AgentRuntime | None = None) -> list[dict]:
    return runtime.state.todos if runtime is not None else CURRENT_TODOS


def _acceptance_items(
    runtime: AgentRuntime | None = None,
) -> list[dict]:
    return [todo for todo in _runtime_todos(runtime)
            if todo.get("kind") == "acceptance"]


def _acceptance_gate_items(
    required: bool,
    runtime: AgentRuntime | None = None,
) -> list[dict]:
    items = _acceptance_items(runtime)
    if required and not items:
        return [{
            "kind": "acceptance",
            "status": "pending",
            "content": "Extract acceptance criteria from the task/README",
        }]
    return [todo for todo in items if todo.get("status") != "completed"]


def _acceptance_review_message(
    items: list[dict], changed_files: set[str], read_budget: int,
    reviewer_output: str = "", reviewer_note: str = "",
) -> str:
    lines = "\n".join(
        f"- [{item.get('id', 'acceptance')} "
        f"{item.get('status', 'pending')}] {item.get('content', '')}"
        for item in items[:12]
    ) or "- (no acceptance criteria were recorded)"
    changed = "\n".join(
        f"- {path}" for path in sorted(changed_files)
    ) or "- (no file changes were recorded)"
    reviewer_section = ""
    if reviewer_output:
        reviewer_section = (
            "\nIndependent reviewer result (fresh context):\n"
            f"<reviewer_result>{reviewer_output}</reviewer_result>\n"
            "Resolve every finding or reject it with concrete code evidence. "
            "Associate unresolved findings with the acceptance checklist. Reuse "
            "the reviewer's files_checked evidence instead of repeating those "
            "reads in the lead context.\n"
        )
    elif reviewer_note:
        reviewer_section = (
            "\nIndependent reviewer status: "
            f"{reviewer_note}. Continue with the bounded Lead audit below; "
            "no reviewer findings were attached.\n"
        )
    return (
        "<acceptance_review>Your final answer is paused for one fresh contract "
        "audit. If no independent reviewer result is attached, re-read the "
        "original task and relevant README/contract with read_file once, then "
        "review only the changed files listed below once. "
        "Do not glob, re-read tests, scan the whole repository, or re-read an "
        "unchanged dependency unless a specific uncovered requirement requires "
        "it. The audit has a strict budget of at most "
        f"{read_budget} read_file calls. Rely on prior context and existing test "
        "notifications. Compare every task-relevant explicit requirement with "
        "the changed code and tests. Treat the current checklist as potentially "
        "incomplete: look for omitted error paths, words such as any/different/"
        "unchanged, and every field named in normalized data. For a derived "
        "value such as a fingerprint, normalized key, hash, or serialized "
        "payload, inspect its producer function and enumerate every contract "
        "field; checking only the caller or comparison site is not evidence. "
        "For any/all requirements, inspect every named exception or state "
        "branch. Public tests alone do not prove requirements they do not "
        "cover. Add any missing "
        "kind=acceptance items, fix uncovered gaps, and call todo_write again "
        "after this audit with concise evidence before producing final.\n"
        "Changed files to review:\n"
        f"{changed}\n"
        "Current checklist:\n"
        f"{lines}\n"
        f"{reviewer_section}</acceptance_review>"
    )


def _acceptance_review_followup_message() -> str:
    return (
        "<acceptance_review>The final contract audit has not been recorded. "
        "Before final, use todo_write to record the audited acceptance checklist "
        "and evidence, including any requirements omitted from the earlier list."
        "</acceptance_review>"
    )


def _append_acceptance_warning(content: list, items: list[dict]):
    lines = "\n".join(
        f"- [{item.get('id', 'acceptance')}] {item.get('content', '')}"
        for item in items[:8])
    content.append({
        "type": "text",
        "text": (
            "\n\n[Acceptance review incomplete]\n"
            "The following requirements were not verified with evidence:\n"
            f"{lines}"
        ),
    })


def _append_contract_audit_warning(content: list):
    content.append({
        "type": "text",
        "text": (
            "\n\n[Acceptance review incomplete]\n"
            "The required final contract audit was not recorded with todo_write; "
            "the completion claim may omit task/README requirements."
        ),
    })


def _tool_json(output) -> dict:
    if isinstance(output, dict):
        return output
    try:
        value = json.loads(str(output))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _reviewer_task_prompt(
    changed_files: set[str], revision: int, checklist: list[dict],
) -> str:
    changed = "\n".join(
        f"- {path}" for path in sorted(changed_files)
    ) or "- (no changed-file path was recorded)"
    requirements = "\n".join(
        f"- {item.get('content', '')}" for item in checklist[:12]
    ) or "- Extract requirements independently from the root task and README."
    return (
        f"Perform the independent pre-final audit for revision {revision}. The "
        "root task and repository README/contract are authoritative; do not limit "
        "the audit to fixes the lead believes it made. Inspect the complete current "
        "changed-file set below and only direct producers/dependencies "
        "needed to verify every contract field and state/error branch. Public tests "
        "alone are insufficient. Return at most five concise structured findings.\n"
        f"Changed files:\n{changed}\n"
        f"Lead checklist (potentially incomplete):\n{requirements}"
    )


def _reviewer_findings(output: str) -> list[dict]:
    envelope = _tool_json(output)
    result = envelope.get("result", {})
    if not isinstance(result, dict):
        return []
    findings = result.get("findings", [])
    return [item for item in findings[:5] if isinstance(item, dict)] \
        if isinstance(findings, list) else []


def _register_reviewer_findings(
    findings: list[dict], revision: int | None = None,
    runtime: AgentRuntime | None = None,
) -> str:
    """Make reviewer concerns part of the locked acceptance state."""
    if not findings:
        return ""
    revision_number = revision if revision is not None else 0
    current_todos = _runtime_todos(runtime)
    registered = []
    registered_ids = []
    for index, finding in enumerate(findings[:5], 1):
        finding_id = f"review:r{revision_number}:f{index}"
        existing = next(
            (todo for todo in current_todos if todo.get("id") == finding_id),
            None,
        )
        if existing is not None:
            registered.append(str(existing.get("content", "")))
            registered_ids.append(finding_id)
            continue
        location = ":".join(filter(None, (
            str(finding.get("file", "")).strip(),
            str(finding.get("symbol", "")).strip(),
        )))
        requirement = str(
            finding.get("requirement", "Reviewer concern")).strip()
        detail = f"{location} {requirement}".strip()
        content = (
            f"Resolve pre-final reviewer finding for revision "
            f"{revision_number}: {detail or 'Reviewer concern'}"
        )[:500]
        acceptance_count = len(_acceptance_items(runtime))
        if len(current_todos) >= 20 or acceptance_count >= 12:
            record_event(
                "acceptance_gate", decision="reviewer_findings_not_registered",
                finding_count=len(findings), registered_count=len(registered),
                reason="todo_capacity", updateable_ids=registered_ids,
            )
            break
        current_todos.append({
            "id": finding_id,
            "content": content,
            "status": "pending",
            "kind": "acceptance",
        })
        registered.append(content)
        registered_ids.append(finding_id)
    record_event(
        "acceptance_gate", decision="reviewer_findings_registered",
        finding_count=len(findings), registered_count=len(registered),
        updateable_ids=registered_ids,
    )
    return "; ".join(registered)


def _reviewer_only_message(output: str, revision: int) -> str:
    return (
        f"<pre_final_review revision=\"{revision}\">An independent reviewer "
        "has audited the current revision from fresh context. Resolve every "
        "finding or reject it with concrete code evidence before final. Do not "
        "repeat the reviewer's repository reads by default.\n"
        f"<reviewer_result>{output}</reviewer_result>"
        "</pre_final_review>"
    )


def _finalization_budget_message(snapshot: dict) -> str:
    return (
        "<finalization_budget>This task has entered its reserved finalization "
        f"budget ({snapshot.get('remaining_calls')} model calls remain; "
        f"{snapshot.get('reserve_calls')} are reserved). Do not start a new "
        "Explorer, Reviewer, Worker, broad repository scan, or model-generated "
        "compact summary. Continue directly from retained evidence. Use the "
        "remaining calls only for unresolved fixes, targeted verification, and "
        "one final answer.</finalization_budget>"
    )


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


def prepare_context(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> list:
    # Every LLM turn enters through the same context budget pipeline.
    messages[:] = _run_context_stage(
        "tool_result_budget", messages,
        lambda value: tool_result_budget(value, runtime=runtime),
    )
    messages[:] = _run_context_stage("micro_compact", messages, micro_compact)
    messages[:] = _run_context_stage("snip_compact", messages, snip_compact)
    if estimate_size(messages) > CONTEXT_LIMIT:
        before = _context_stats(messages)
        messages[:] = (
            compact_history(messages, runtime=runtime)
            if runtime is not None else compact_history(messages)
        )
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
    notes = collect_background_results()
    _record_background_notifications(notes, "tool_result_batch")
    for note in notes:
        content.append({"type": "text", "text": note})
    return content


def _record_background_notifications(notes: list, injection: str):
    for note in notes:
        record_event(
            "task_notification",
            injection=injection,
            task_id=getattr(note, "task_id", ""),
            status=getattr(note, "status", ""),
            command=getattr(note, "command", ""),
            summary=getattr(note, "summary", str(note)),
            original_size=getattr(note, "original_size", len(str(note))),
            truncated=bool(getattr(note, "truncated", False)),
        )


def inject_background_notifications(messages: list):
    notes = collect_background_results()
    if notes:
        _record_background_notifications(notes, "loop_start")
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


def todo_required_message(acceptance_required: bool = False) -> str:
    message = ("Tool not run: before changing files, this multi-step task needs "
               "a todo list. Inspect the task/README with read-only tools first "
               "if needed, then call todo_write with a short plan")
    if acceptance_required:
        message += (" and concrete kind=acceptance items extracted from those "
                    "requirements")
    return message + ", then continue with the file change."


def acceptance_required_message() -> str:
    return (
        "Tool not run: before changing files, add concrete kind=acceptance "
        "items from the task/README requirements with todo_write. Read-only "
        "contract discovery may continue first."
    )


def _todo_state_summary(runtime: AgentRuntime | None = None) -> str:
    current_todos = _runtime_todos(runtime)
    acceptance = _acceptance_items(runtime)
    unverified = [todo for todo in acceptance
                  if todo.get("status") != "completed"]
    detail = ""
    if acceptance:
        detail = (f" ({len(acceptance)} acceptance, "
                  f"{len(unverified)} unverified)")
    ids = [str(todo.get("id")) for todo in acceptance if todo.get("id")]
    id_detail = f"; acceptance IDs: {', '.join(ids)}" if ids else ""
    return f"Updated {len(current_todos)} todos{detail}{id_detail}"


def _reconcile_locked_acceptance(
    previous_todos: list[dict],
    runtime: AgentRuntime | None = None,
) -> tuple[list[str], str | None]:
    """Keep established contract items without turning wording drift into a retry."""
    previous = [dict(todo) for todo in previous_todos
                if todo.get("kind") == "acceptance"]
    current_todos = _runtime_todos(runtime)
    current = [todo for todo in current_todos
               if todo.get("kind") == "acceptance"]
    if not previous:
        return [], None

    previous_contents = {todo.get("content") for todo in previous}
    current_contents = {todo.get("content") for todo in current}
    current_by_id = {
        str(todo["id"]): todo for todo in current if todo.get("id")
    }
    notices = []

    # Stable IDs are authoritative. They let the model update a reviewer
    # finding with evidence without reproducing generated wording.
    for old_item in previous:
        old_id = str(old_item.get("id", ""))
        candidate = current_by_id.get(old_id) if old_id else None
        if candidate is None:
            continue
        candidate["content"] = old_item.get("content", "")
        candidate["kind"] = "acceptance"

    # Models often rephrase an item while keeping the same list shape. Treat
    # that as a status/evidence update and retain the stable contract wording.
    if len(previous) == len(current):
        for index, old_item in enumerate(previous):
            old_content = old_item.get("content")
            if old_content in current_contents:
                continue
            candidate = current[index]
            candidate_content = candidate.get("content")
            if candidate_content in previous_contents:
                continue
            candidate["content"] = old_content
            if old_item.get("id"):
                candidate["id"] = old_item["id"]
            current_contents.discard(candidate_content)
            current_contents.add(old_content)
            notices.append(f"kept wording: {old_content}")

    current_contents = {
        todo.get("content") for todo in current_todos
        if todo.get("kind") == "acceptance"
    }
    restored = [todo for todo in previous
                if todo.get("content") not in current_contents
                and (not todo.get("id")
                     or str(todo.get("id")) not in {
                         str(current.get("id")) for current in current_todos
                         if current.get("id")
                     })]
    if restored:
        current_todos.extend(dict(todo) for todo in restored)
        notices.extend(
            f"restored omitted item: {todo.get('content')}" for todo in restored)

    acceptance_count = sum(
        1 for todo in current_todos if todo.get("kind") == "acceptance")
    if len(current_todos) > 20 or acceptance_count > 12:
        current_todos[:] = [dict(todo) for todo in previous_todos]
        return [], (
            "Error: todo update exceeds limits after preserving locked "
            "acceptance items; keep existing criteria and add fewer new items. "
            "Updateable acceptance IDs: "
            + ", ".join(
                str(todo.get("id")) for todo in previous if todo.get("id"))
        )
    return notices, None


def _runtime_role_benefit(read_counts: dict[str, int], model_client) -> dict:
    """Describe a conservative, evidence-based opportunity for one Explorer."""
    unique_paths = len(read_counts)
    repeated_reads = sum(max(0, count - 1) for count in read_counts.values())
    scopes = set()
    for path in read_counts:
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        if parts and parts[0].lower() == "workspace":
            parts = parts[1:]
        scopes.add(parts[0] if len(parts) > 1 else "(root)")
    repeated_paths = sorted(
        path for path, count in read_counts.items() if count > 1)
    evidence_ready = (
        unique_paths >= 8 and repeated_reads >= 2 and len(scopes) >= 2)
    budget_allowed = False
    budget = {"available": False}
    if evidence_ready:
        # Two focused Explorer calls plus a three-call Reviewer allowance must
        # fit before the existing finalization reserve.
        budget_allowed, budget = can_spend_optional_calls(model_client, 5)
    return {
        "eligible": evidence_ready and budget_allowed,
        "evidence_ready": evidence_ready,
        "budget_allowed": budget_allowed,
        "unique_read_paths": unique_paths,
        "repeated_reads": repeated_reads,
        "scope_count": len(scopes),
        "repeated_paths": repeated_paths[:4],
        "budget": budget,
    }


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
             state: RecoveryState, max_tokens: int,
             runtime: AgentRuntime | None = None):
    remaining = _remaining_case_time(runtime)
    if remaining is not None and remaining <= 0:
        raise _CaseTimeoutError("eval case deadline exceeded")
    system = (
        assemble_system_prompt(context, runtime)
        if runtime is not None else assemble_system_prompt(context)
    )
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
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
            lambda: model_client.messages.create(
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


def _remaining_case_time(
    runtime: AgentRuntime | None = None,
) -> float | None:
    deadline = runtime.state.deadline if runtime is not None else CASE_DEADLINE
    if deadline is None:
        return None
    return deadline - _time.monotonic()


def _check_case_deadline(runtime: AgentRuntime | None = None):
    remaining = _remaining_case_time(runtime)
    if remaining is not None and remaining <= 0:
        if background_workers_alive():
            raise _CaseTimeoutError(
                "eval case deadline exceeded while waiting for background tasks")
        raise _CaseTimeoutError("eval case deadline exceeded")


def agent_loop(
    messages: list,
    context: dict,
    runtime: AgentRuntime | None = None,
):
    global rounds_since_todo
    from . import bootstrap
    bootstrap()
    tools, handlers = (
        assemble_tool_pool(runtime)
        if runtime is not None else assemble_tool_pool()
    )
    state = RecoveryState()
    if runtime is not None:
        state.current_model = runtime.config.primary_model
    max_tokens = DEFAULT_MAX_TOKENS
    # Todos are scoped to one user/cron turn. Live acceptance state remains
    # available through every context compaction inside this loop.
    current_todos = _runtime_todos(runtime)
    current_todos.clear()
    acceptance_required = requires_acceptance_todos(messages)
    todo_required = requires_initial_todo(messages) or acceptance_required
    todo_started = False
    acceptance_locked = False
    acceptance_review_prompted = False
    acceptance_review_todo_updated = False
    acceptance_review_followup_prompted = False
    changed_file_paths = (
        runtime.state.changed_files if runtime is not None else set()
    )
    changed_file_paths.clear()
    lead_read_paths: set[str] = set()
    lead_read_counts = (
        runtime.state.lead_read_counts if runtime is not None else {}
    )
    lead_read_counts.clear()
    audit_read_paths: set[str] = set()
    audit_read_budget = 0
    root_task = str(
        runtime.state.root_task if runtime is not None
        else CURRENT_ROOT_TASK or _latest_user_instruction(messages)
    )
    if not root_task:
        root_task = _latest_user_instruction(messages)
    complexity = assess_task_complexity(root_task)
    multiagent_enabled = "delegate_agent" in handlers
    multiagent_required = (
        multiagent_enabled
        and complexity["level"] == "complex"
        and complexity.get("implementation_task", False)
    )
    explorer_attempted = False
    runtime_benefit_signal_sent = False
    explorer_cached_result = ""
    mutation_revision = 0
    reviewer_attempted_revision = -1
    reviewer_cached_result = ""
    finalization_budget_notice_sent = False
    budget_snapshot_observed = False
    if multiagent_required:
        messages.append({
            "role": "user", "content": complex_delegation_briefing(complexity),
        })
        record_event(
            "multiagent_policy", decision="advisory", **complexity)

    while True:
        _check_case_deadline(runtime)
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

        model_client = (
            runtime.services.model_client if runtime is not None else client
        )
        reserve_active, budget_snapshot = finalization_reserve_active(
            model_client)
        if budget_snapshot.get("available") and not budget_snapshot_observed:
            budget_snapshot_observed = True
            record_event(
                "model_budget_guard", decision="budget_snapshot_available",
                **{key: value for key, value in budget_snapshot.items()
                   if key != "available"},
            )
        if (budget_snapshot.get("available")
                and budget_snapshot["remaining_calls"] <= 0):
            unresolved = _acceptance_gate_items(acceptance_required, runtime)
            fallback = (
                "Harness stopped before issuing an over-budget model request. "
                "The implementation changes made so far remain in the workspace."
            )
            if unresolved:
                fallback += " Unresolved acceptance work: " + "; ".join(
                    str(item.get("content", ""))[:180]
                    for item in unresolved[:5]
                )
            record_event(
                "model_budget_guard", decision="over_budget_request_prevented",
                unresolved_count=len(unresolved),
                **{key: value for key, value in budget_snapshot.items()
                   if key != "available"},
            )
            record_hook("Stop")
            trigger_hooks("Stop", messages)
            finish_run(fallback)
            return

        force_final_response = bool(
            budget_snapshot.get("available")
            and budget_snapshot["remaining_calls"] == 1
        )
        if reserve_active and not finalization_budget_notice_sent:
            messages.append({
                "role": "user",
                "content": _finalization_budget_message(budget_snapshot),
            })
            finalization_budget_notice_sent = True
            record_event(
                "model_budget_guard", decision="finalization_reserve_entered",
                **{key: value for key, value in budget_snapshot.items()
                   if key != "available"},
            )
        if force_final_response:
            unresolved = _acceptance_gate_items(acceptance_required, runtime)
            messages.append({
                "role": "user",
                "content": (
                    "<finalization_deadline>Exactly one model call remains. "
                    "Tools are disabled for this call. Return the best accurate "
                    "final answer now from retained evidence; state any unfinished "
                    "acceptance work honestly and do not request another action."
                    + (
                        " Unresolved acceptance work: " + "; ".join(
                            str(item.get("content", ""))[:180]
                            for item in unresolved[:5]
                        )
                        if unresolved else ""
                    )
                    + "</finalization_deadline>"
                ),
            })
            record_event(
                "model_budget_guard", decision="last_call_forced_final",
                unresolved_count=len(unresolved),
                **{key: value for key, value in budget_snapshot.items()
                   if key != "available"},
            )

        if runtime is not None:
            prepare_context(messages, runtime)
            context = update_context(context, messages, runtime)
            tools, handlers = assemble_tool_pool(runtime)
        else:
            prepare_context(messages)
            context = update_context(context, messages)
            tools, handlers = assemble_tool_pool()
        if force_final_response:
            tools = []

        try:
            response = (
                call_llm(messages, context, tools, state, max_tokens, runtime)
                if runtime is not None
                else call_llm(messages, context, tools, state, max_tokens)
            )
            record_llm_response(response)
        except Exception as e:
            if isinstance(e, _CaseTimeoutError):
                raise
            record_error(e)
            if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                messages[:] = (
                    reactive_compact(messages, runtime)
                    if runtime is not None else reactive_compact(messages)
                )
                state.has_attempted_reactive_compact = True
                continue
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        if response.stop_reason == "max_tokens":
            if force_final_response:
                messages.append({"role": "assistant", "content": response.content})
                record_hook("Stop")
                trigger_hooks("Stop", messages)
                finish_run(extract_text(response.content))
                return
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
        if force_final_response:
            unresolved = _acceptance_gate_items(acceptance_required, runtime)
            if unresolved:
                _append_acceptance_warning(response.content, unresolved)
            record_hook("Stop")
            trigger_hooks("Stop", messages)
            finish_run(extract_text(response.content))
            return
        if not has_tool_use(response.content):
            if background_workers_alive() and CASE_DEADLINE is not None:
                remaining = _remaining_case_time(runtime)
                if not wait_for_background_tasks(remaining):
                    raise _CaseTimeoutError(
                        "eval case deadline exceeded while waiting for background tasks")
            # A worker may finish while the model is producing its final text,
            # so collect completed notifications even when no thread is alive
            # by the time this branch is reached.
            notes = collect_background_results()
            if notes:
                _record_background_notifications(notes, "final_wait")
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
            remaining = _remaining_case_time(runtime)
            notification_wait = 2.0 if remaining is None else max(0, min(2.0, remaining))
            if wait_for_imminent_once(notification_wait):
                continue
            needs_acceptance_review = (
                acceptance_required and not acceptance_review_prompted
            )
            needs_independent_reviewer = (
                multiagent_required and mutation_revision > 0
                and reviewer_attempted_revision != mutation_revision
            )
            if needs_acceptance_review or needs_independent_reviewer:
                checklist = _acceptance_items(runtime)
                if needs_acceptance_review:
                    audit_read_paths.clear()
                    audit_read_budget = max(
                        4, min(8, len(changed_file_paths) + 3))
                    acceptance_review_prompted = True
                    acceptance_review_todo_updated = False
                    acceptance_review_followup_prompted = False

                reviewer_output = (
                    reviewer_cached_result
                    if reviewer_attempted_revision == mutation_revision else ""
                )
                reviewer_attached = bool(reviewer_output)
                reviewer_note = ""
                if needs_independent_reviewer:
                    reviewer_allowed, reviewer_budget = can_spend_optional_calls(
                        model_client, 3)
                    if reviewer_allowed:
                        record_event(
                            "multiagent_policy", decision="reviewer_auto_started",
                            mutation_revision=mutation_revision,
                            **{key: value for key, value in reviewer_budget.items()
                               if key != "available"},
                        )
                        reviewer_output = call_tool_handler(
                            handlers.get("delegate_agent"),
                            {
                                "role": "reviewer",
                                "prompt": _reviewer_task_prompt(
                                    changed_file_paths,
                                    mutation_revision,
                                    checklist,
                                ),
                                "name": f"pre-final-review-{mutation_revision}",
                            },
                            "delegate_agent",
                        )
                        reviewer_attached = True
                    else:
                        reviewer_cached_result = json.dumps({
                            "status": "budget_reserved",
                            "role": "reviewer",
                            "verdict": "blocked",
                            "error": (
                                "Independent reviewer skipped to preserve calls "
                                "for direct fixes, verification, and final."
                            ),
                            "budget": {
                                key: value for key, value in reviewer_budget.items()
                                if key != "available"
                            },
                        })
                        reviewer_output = ""
                        reviewer_attached = False
                        reviewer_note = (
                            "skipped to preserve the finalization call reserve")
                        record_event(
                            "model_budget_guard",
                            decision="automatic_reviewer_skipped",
                            mutation_revision=mutation_revision,
                            **{key: value for key, value in reviewer_budget.items()
                               if key != "available"},
                        )
                    reviewer_attempted_revision = mutation_revision
                    if reviewer_attached:
                        reviewer_cached_result = str(reviewer_output)
                    delegation = _tool_json(reviewer_output)
                    verdict = str(delegation.get("verdict", "")).lower()
                    findings = _reviewer_findings(reviewer_output)
                    if findings:
                        if not acceptance_required:
                            acceptance_required = True
                            todo_required = True
                        if not acceptance_review_prompted:
                            audit_read_paths.clear()
                            audit_read_budget = max(
                                4, min(8, len(changed_file_paths) + 3))
                            acceptance_review_prompted = True
                            acceptance_review_todo_updated = False
                            acceptance_review_followup_prompted = False
                            needs_acceptance_review = True
                        _register_reviewer_findings(
                            findings, mutation_revision, runtime)
                        checklist = _acceptance_items(runtime)
                    if reviewer_attached:
                        record_event(
                            "multiagent_policy", decision="reviewer_auto_observed",
                            verdict=verdict or "unknown",
                            finding_count=len(findings),
                            mutation_revision=mutation_revision,
                        )

                if needs_acceptance_review:
                    prompt = _acceptance_review_message(
                        checklist, changed_file_paths, audit_read_budget,
                        reviewer_output=str(reviewer_output),
                        reviewer_note=reviewer_note,
                    )
                elif reviewer_attached:
                    prompt = _reviewer_only_message(
                        str(reviewer_output), mutation_revision)
                else:
                    prompt = ""
                if prompt:
                    messages.append({
                        "role": "user",
                        "content": prompt,
                    })
                if needs_acceptance_review:
                    record_event(
                        "acceptance_gate", decision="pre_final_review",
                        checklist_count=len(checklist),
                        changed_file_count=len(changed_file_paths),
                        read_budget=audit_read_budget,
                        reviewer_attached=reviewer_attached,
                        reviewer_status=(
                            "attached" if reviewer_attached else "skipped_budget"),
                        unresolved_count=sum(
                            1 for item in checklist
                            if item.get("status") != "completed"),
                    )
                if prompt:
                    continue
            if (acceptance_required and acceptance_review_prompted
                    and not acceptance_review_todo_updated):
                if not acceptance_review_followup_prompted:
                    messages.append({
                        "role": "user",
                        "content": _acceptance_review_followup_message(),
                    })
                    acceptance_review_followup_prompted = True
                    record_event(
                        "acceptance_gate", decision="review_followup",
                    )
                    continue
                _append_contract_audit_warning(response.content)
                record_event(
                    "acceptance_gate", decision="audit_incomplete_final",
                )
            unresolved_acceptance = _acceptance_gate_items(
                acceptance_required, runtime)
            if unresolved_acceptance:
                _append_acceptance_warning(
                    response.content, unresolved_acceptance)
                record_event(
                    "acceptance_gate", decision="incomplete_final",
                    unresolved_count=len(unresolved_acceptance),
                )
            record_hook("Stop")
            trigger_hooks("Stop", messages)
            finish_run(extract_text(response.content))
            return

        results = []
        compacted_now = False
        for block in response.content:
            _check_case_deadline(runtime)
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            record_tool_use(block)

            mutation_requested = block.name in _MAIN_MUTATION_TOOLS
            delegated_role = ""
            if block.name == "delegate_agent":
                delegated_role = str(
                    block.input.get("role", "")).strip().lower()
            elif block.name == "task":
                delegated_role = classify_delegation_intent(
                    block.input.get("description", ""))["role"]
            worker_requested = delegated_role == "worker"
            implementation_requested = mutation_requested or worker_requested
            if implementation_requested and todo_required and not todo_started:
                output = todo_required_message(acceptance_required)
                record_event("todo_gate", tool=block.name,
                             tool_use_id=block.id, input=block.input,
                             reason=output)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue

            if block.name in {"delegate_agent", "task"} \
                    and delegated_role == "explorer" \
                    and explorer_attempted:
                cached = _tool_json(explorer_cached_result)
                cached["reused"] = True
                output = json.dumps(cached)
                record_event(
                    "delegation_reused", agent_role="explorer",
                    tool_use_id=block.id,
                )
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue
            if (block.name in {"delegate_agent", "task"}
                    and delegated_role == "reviewer"
                    and reviewer_attempted_revision == mutation_revision):
                cached = _tool_json(reviewer_cached_result)
                cached["reused"] = True
                output = json.dumps(cached)
                record_event(
                    "delegation_reused", agent_role="reviewer",
                    tool_use_id=block.id, mutation_revision=mutation_revision,
                )
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue
            if (implementation_requested and acceptance_required
                    and not _acceptance_items(runtime)):
                output = acceptance_required_message()
                record_event("todo_gate", tool=block.name,
                             tool_use_id=block.id, input=block.input,
                             reason=output)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue

            audit_active = (
                acceptance_review_prompted
                and not acceptance_review_todo_updated
            )
            if audit_active and block.name == "glob":
                known_paths = ["README.md", *sorted(changed_file_paths)]
                output = (
                    "Tool not run: final contract audit is scoped to the "
                    "original task/README and recorded changed files; repository "
                    "glob scans are disabled. Call read_file directly only if a "
                    "listed path still needs its one allowed audit read. Known "
                    "paths: " + ", ".join(known_paths[:10]) + ". Use prior "
                    "context, then update the acceptance checklist with todo_write."
                )
                record_event(
                    "acceptance_gate", decision="audit_glob_blocked",
                    tool=block.name, tool_use_id=block.id, input=block.input,
                    reason=output,
                )
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue
            if audit_active and block.name == "read_file":
                audit_path = _os.path.normpath(
                    str(block.input.get("path", "")).strip()
                ).replace("\\", "/").lower()
                if audit_path in audit_read_paths:
                    output = (
                        "Tool not run: this path was already read during the "
                        "final contract audit. Use the retained result instead "
                        "of re-reading it."
                    )
                    decision = "audit_read_deduplicated"
                elif len(audit_read_paths) >= audit_read_budget:
                    output = (
                        "Tool not run: final contract audit read budget reached. "
                        "Use the retained context and update the acceptance "
                        "checklist with todo_write."
                    )
                    decision = "audit_read_budget_reached"
                else:
                    audit_read_paths.add(audit_path)
                    output = ""
                    decision = ""
                if decision:
                    record_event(
                        "acceptance_gate", decision=decision,
                        tool=block.name, tool_use_id=block.id,
                        input=block.input, reason=output,
                        read_count=len(audit_read_paths),
                        read_budget=audit_read_budget,
                    )
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": output})
                    record_tool_result(block.id, block.name, output)
                    continue

            if block.name == "compact":
                messages[:] = (
                    compact_history(messages, runtime=runtime)
                    if runtime is not None else compact_history(messages)
                )
                output = "[Compacted. Continue with summarized context.]"
                compact_tool_use_preserved = any(
                    candidate.get("role") == "assistant"
                    and any(block_type(item) == "tool_use"
                            and ((item.get("id") if isinstance(item, dict)
                                  else getattr(item, "id", None)) == block.id)
                            for item in candidate.get("content", []))
                    for candidate in messages
                )
                if compact_tool_use_preserved:
                    messages.append({"role": "user", "content": [{
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }]})
                else:
                    messages.append({"role": "user", "content": output})
                record_tool_result(block.id, block.name, output)
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
                routing_reason = (
                    "explicit" if block.input.get("run_in_background")
                    else "slow_command"
                )
                record_event(
                    "background_routed",
                    tool=block.name,
                    tool_use_id=block.id,
                    command=block.input.get("command", ""),
                    reason=routing_reason,
                )
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
            todo_snapshot = (
                [dict(todo) for todo in current_todos]
                if block.name == "todo_write" and acceptance_locked else None
            )
            output = call_tool_handler(handler, block.input, block.name)
            if (block.name == "todo_write" and todo_snapshot is not None
                    and not str(output).startswith("Error:")):
                notices, reconciliation_error = _reconcile_locked_acceptance(
                    todo_snapshot, runtime)
                if reconciliation_error:
                    output = reconciliation_error
                elif notices:
                    output = (_todo_state_summary(runtime)
                              + "\nProtected acceptance criteria preserved: "
                              + "; ".join(notices))
            trigger_hooks("PostToolUse", block, output)
            record_hook("PostToolUse", tool=block.name)
            print(str(output)[:300])

            if (block.name in {"delegate_agent", "task"}
                    and not str(output).startswith("Error:")):
                delegation = _tool_json(output)
                verdict = str(delegation.get("verdict", "")).lower()
                if delegated_role == "explorer":
                    explorer_attempted = True
                    explorer_cached_result = str(output)
                    record_event(
                        "multiagent_policy", decision="explorer_observed",
                        verdict=verdict or "unknown",
                    )
                elif delegated_role == "reviewer":
                    reviewer_attempted_revision = mutation_revision
                    reviewer_cached_result = str(output)
                    findings = _reviewer_findings(str(output))
                    if findings:
                        if not acceptance_required:
                            acceptance_required = True
                            todo_required = True
                        _register_reviewer_findings(
                            findings, mutation_revision, runtime)
                    record_event(
                        "multiagent_policy",
                        decision=("reviewer_pass" if (
                            delegation.get("status") == "completed"
                            and verdict == "pass" and not findings
                        ) else "reviewer_observed"),
                        verdict=verdict or "unknown",
                        finding_count=len(findings),
                        mutation_revision=mutation_revision,
                    )

            if block.name == "todo_write":
                rounds_since_todo = 0
                if not str(output).startswith("Error:"):
                    todo_started = True
                    if acceptance_review_prompted:
                        acceptance_review_todo_updated = True
                if (not str(output).startswith("Error:")
                        and acceptance_required
                        and not _acceptance_items(runtime)):
                    output += (
                        "\nAcceptance checklist required before the first file "
                        "change; continue read-only contract discovery or add at "
                        "least one kind=acceptance item.")
            else:
                rounds_since_todo += 1
                mutation_succeeded = (
                    mutation_requested
                    and not str(output).lower().startswith((
                        "error:", "permission denied", "tool not run"))
                )
                integration = _tool_json(output) if block.name == "integrate_worktree" else {}
                if block.name == "integrate_worktree":
                    mutation_succeeded = integration.get("status") == "integrated"
                if mutation_succeeded:
                    acceptance_locked = True
                    mutation_revision += 1
                    if multiagent_required and acceptance_review_prompted:
                        acceptance_review_prompted = False
                        acceptance_review_todo_updated = False
                        acceptance_review_followup_prompted = False
                    changed_path = str(block.input.get("path", "")).strip()
                    if changed_path:
                        changed_file_paths.add(changed_path)
                    for changed_path in integration.get("changed_files", []):
                        if changed_path:
                            changed_file_paths.add(str(changed_path))

            if (block.name == "read_file"
                    and not str(output).lower().startswith("error:")):
                read_path = _os.path.normpath(
                    str(block.input.get("path", "")).strip()
                ).replace("\\", "/").lower()
                if read_path:
                    lead_read_paths.add(read_path)
                    lead_read_counts[read_path] = (
                        lead_read_counts.get(read_path, 0) + 1)
                if (multiagent_enabled and not runtime_benefit_signal_sent
                        and not explorer_attempted
                        and complexity.get("implementation_task", False)):
                    benefit = _runtime_role_benefit(
                        lead_read_counts, model_client)
                    if benefit["evidence_ready"]:
                        runtime_benefit_signal_sent = True
                        event_budget = {
                            key: value for key, value in benefit["budget"].items()
                            if key != "available"
                        }
                        if benefit["eligible"]:
                            record_event(
                                "multiagent_policy",
                                decision="runtime_benefit_observed",
                                unique_read_paths=benefit["unique_read_paths"],
                                repeated_reads=benefit["repeated_reads"],
                                scope_count=benefit["scope_count"],
                                repeated_paths=benefit["repeated_paths"],
                                **event_budget,
                            )
                        else:
                            record_event(
                                "multiagent_policy",
                                decision="runtime_benefit_observed_no_budget",
                                unique_read_paths=benefit["unique_read_paths"],
                                repeated_reads=benefit["repeated_reads"],
                                scope_count=benefit["scope_count"],
                                **event_budget,
                            )

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


def cron_autorun_loop(
    history: list,
    context: dict,
    runtime: AgentRuntime | None = None,
):
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
            if runtime is not None:
                runtime.state.root_task = scheduled_prompt
            start_run(scheduled_prompt, workdir=WORKDIR,
                      model_provider=MODEL_PROVIDER, model=MODEL)
            try:
                if runtime is not None:
                    agent_loop(history, context, runtime)
                    context.update(update_context(context, history, runtime))
                else:
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
        "CURRENT_ROOT_TASK", "BACKGROUND_TASKS_ENABLED", "APPROVAL_MODE",
        *_WORKDIR_DERIVED_PATHS,
    ]
    old_state = {name: _runtime_value(name) for name in state_names}
    run = None
    runtime = None
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
        _set_runtime_value("CURRENT_ROOT_TASK", task)
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
        runtime = AgentRuntime.create(
            workdir=workdir_path,
            state_root=runtime_root,
            model_client=_runtime_value("client"),
            command_executor=_runtime_value("COMMAND_EXECUTOR"),
            model_provider=provider_name,
            model=model_name,
            primary_model=model_name,
            fallback_model=_runtime_value("FALLBACK_MODEL"),
            tool_policy=tool_policy,
            approval_mode=_runtime_value("APPROVAL_MODE"),
            background_tasks_enabled=bool(
                _runtime_value("BACKGROUND_TASKS_ENABLED")),
            root_task=task,
            deadline=case_deadline,
        )
        run = start_run(task, workdir=workdir_path,
                        model_provider=provider_name, model=model_name,
                        storage_root=(_Path(trace_storage_root).resolve()
                                      if trace_storage_root else None))
        record_hook("UserPromptSubmit", input=task)
        trigger_hooks("UserPromptSubmit", task)
        if tool_policy:
            record_event("tool_policy", **tool_policy)

        messages = [{"role": "user", "content": task}]
        runtime.services.trace_recorder = run
        context = update_context({}, [], runtime)
        if manage_lifecycle:
            start_scheduler(load_durable=True)
        if command_executor is not None:
            command_executor.start()
        with agent_lock:
            agent_loop(messages, context, runtime)
            update_context(context, messages, runtime)
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
