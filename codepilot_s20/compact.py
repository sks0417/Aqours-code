from __future__ import annotations

import hashlib
import json
import time
from copy import copy as shallow_copy
from pathlib import Path

from .model_budget import can_spend_optional_calls
from .runtime import AgentRuntime
from .runtime_state import *


# Compaction is intentionally a small sliding-window mechanism: one cumulative
# Markdown checkpoint plus a recent verbatim suffix. Token counts are estimates
# because supported providers do not share a tokenizer.
CONTEXT_CHECKPOINT_MARKER = "[Context checkpoint]"
LATEST_USER_INSTRUCTION_MARKER = "[Latest user instruction — verbatim]"
LATEST_USER_INSTRUCTION_END_MARKER = "[/Latest user instruction]"
COMPACT_TRIGGER_RATIO = 0.85
RECENT_TOOL_RESULT_COUNT = 4
# A 6k-token raw suffix plus the reserved 2k-token checkpoint leaves roughly
# 6k estimated tokens for system/tool schemas in the 50,000-character window.
RECENT_TAIL_MAX_TOKENS = 6_000
MAX_TOOL_RESULT_TOKENS = 6_000
SUMMARY_MAX_TOKENS = 2_000
ESTIMATED_CHARS_PER_TOKEN = 3
COMPACT_OUTPUT_RESERVE_CHARS = 6_000
SUMMARY_OUTPUT_RESERVE_CHARS = (
    SUMMARY_MAX_TOKENS * ESTIMATED_CHARS_PER_TOKEN
)

COMPACTION_PROMPT = """\
You are creating a context checkpoint for another coding-agent model
that will continue the current task.

Summarize only the supplied older conversation history into one concise,
self-contained Markdown continuation handoff. Recent messages remain available
verbatim outside this summary.

Preserve:
- the user's final goal, explicit constraints, and acceptance conditions
- completed work, modified files, current focus, and remaining work
- important decisions and the reasons for them
- concrete conclusions learned from important files and tool results
- relevant paths, classes, functions, symbols, configuration, interfaces,
  behavior, constraints, and cross-file relationships
- commands and tests run, including exact outcomes
- errors, failed attempts, and their causes when still relevant
- unresolved problems and explicit next steps

Do not merely state that a file was inspected.
Preserve the concrete conclusions learned from it, including relevant
paths, symbols, behavior, constraints, errors, commands, test results,
decisions, and unresolved work.

If an earlier context checkpoint is present, merge it with the newer history:
preserve facts that remain true, remove stale facts, and return one replacement
checkpoint. Do not emit archive IDs, manifests, checkpoint IDs, or disk-recovery
instructions. The result must not depend on tool results that are no longer in
the active context.

Do not answer the original task. Do not describe the summarization process.
Do not invent facts. Return concise Markdown only.
"""


def estimate_size(messages: list) -> int:
    """Return a deterministic serialized-size estimate in characters."""
    return len(json.dumps(
        messages,
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
    ))


def estimate_context_size(
    messages: list,
    *,
    system: str = "",
    tools: list | None = None,
    dynamic: dict | None = None,
) -> int:
    """Estimate all assembled request components in characters."""
    return len(json.dumps(
        {
            "system": system,
            "messages": messages,
            "tools": tools or [],
            "dynamic": dynamic or {},
        },
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
    ))


def estimate_context_tokens(size_chars: int) -> int:
    """Conservatively convert serialized characters to estimated tokens."""
    return (
        max(0, int(size_chars)) + ESTIMATED_CHARS_PER_TOKEN - 1
    ) // ESTIMATED_CHARS_PER_TOKEN


def block_type(block):
    return (
        block.get("type")
        if isinstance(block, dict)
        else getattr(block, "type", None)
    )


def _block_field(block, name: str, default=None):
    return (
        block.get(name, default)
        if isinstance(block, dict)
        else getattr(block, name, default)
    )


def _copy_content(content):
    if isinstance(content, list):
        copied = []
        for block in content:
            if isinstance(block, dict):
                copied.append(dict(block))
            else:
                try:
                    copied.append(shallow_copy(block))
                except Exception:
                    copied.append(block)
        return copied
    # Runtime notification strings may be subclasses with required __new__
    # arguments. They are immutable, so retaining them is safe.
    return content


def _copy_messages(messages: list) -> list:
    copied = []
    for message in messages:
        if not isinstance(message, dict):
            copied.append(message)
            continue
        item = dict(message)
        item["content"] = _copy_content(message.get("content"))
        copied.append(item)
    return copied


def message_has_tool_use(message: dict) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "assistant"
        and isinstance(content, list)
        and any(block_type(block) == "tool_use" for block in content)
    )


def is_tool_result_message(message: dict) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and any(block_type(block) == "tool_result" for block in content)
    )


def collect_tool_results(messages: list):
    found = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if block_type(block) == "tool_result":
                found.append((message_index, block_index, block))
    return found


def _tool_use_ids(message: dict) -> set[str]:
    if not message_has_tool_use(message):
        return set()
    return {
        str(_block_field(block, "id", ""))
        for block in message["content"]
        if block_type(block) == "tool_use"
        and _block_field(block, "id", "")
    }


def _tool_result_ids(message: dict) -> set[str]:
    if not is_tool_result_message(message):
        return set()
    return {
        str(_block_field(block, "tool_use_id", ""))
        for block in message["content"]
        if block_type(block) == "tool_result"
        and _block_field(block, "tool_use_id", "")
    }


def _history_units(messages: list) -> list[list[dict]]:
    """Group messages at boundaries that never split a tool exchange."""
    units: list[list[dict]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message_has_tool_use(message) and index + 1 < len(messages):
            following = messages[index + 1]
            use_ids = _tool_use_ids(message)
            result_ids = _tool_result_ids(following)
            if result_ids and result_ids <= use_ids:
                units.append([message, following])
                index += 2
                continue
        units.append([message])
        index += 1
    return units


def _is_tool_exchange_unit(unit: list[dict]) -> bool:
    return (
        len(unit) == 2
        and message_has_tool_use(unit[0])
        and is_tool_result_message(unit[1])
    )


def _is_checkpoint_message(message: dict) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return content.lstrip().startswith(CONTEXT_CHECKPOINT_MARKER)
    if isinstance(content, list):
        first_text = next(
            (
                str(_block_field(block, "text", ""))
                for block in content
                if block_type(block) == "text"
            ),
            "",
        )
        return first_text.lstrip().startswith(CONTEXT_CHECKPOINT_MARKER)
    return False


def _is_user_instruction(message: dict) -> bool:
    if (
        message.get("role") != "user"
        or is_tool_result_message(message)
        or _is_checkpoint_message(message)
    ):
        return False
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
        return bool(text) and not text.startswith("<task_notification>")
    if not isinstance(content, list):
        return bool(str(content).strip())
    return any(
        block_type(block) == "text"
        and str(_block_field(block, "text", "")).strip()
        and not str(_block_field(block, "text", "")).strip().startswith(
            "<task_notification>"
        )
        for block in content
    )


def _extract_pinned_user_instruction(message: dict):
    """Extract only the mechanically delimited verbatim checkpoint section."""
    if not _is_checkpoint_message(message):
        return None
    content = message.get("content")
    if isinstance(content, str):
        start = "\n" + LATEST_USER_INSTRUCTION_MARKER + "\n"
        end = "\n" + LATEST_USER_INSTRUCTION_END_MARKER
        start_at = content.rfind(start)
        if start_at < 0 or not content.endswith(end):
            return None
        return content[start_at + len(start):-len(end)]
    if not isinstance(content, list):
        return None
    start_at = -1
    for index, block in enumerate(content):
        if (
            block_type(block) == "text"
            and str(_block_field(block, "text", ""))
            == LATEST_USER_INSTRUCTION_MARKER
        ):
            start_at = index
    if start_at < 0:
        return None
    for index in range(start_at + 1, len(content)):
        block = content[index]
        if (
            block_type(block) == "text"
            and str(_block_field(block, "text", ""))
            == LATEST_USER_INSTRUCTION_END_MARKER
        ):
            if index == start_at + 1:
                return None
            return _copy_content(content[start_at + 1:index])
    return None


def _latest_user_instruction(messages: list) -> tuple[object | None, int | None]:
    """Return the newest real instruction, or the prior pinned instruction."""
    for message in reversed(messages):
        if _is_user_instruction(message):
            return _copy_content(message.get("content")), id(message)
    for message in reversed(messages):
        pinned = _extract_pinned_user_instruction(message)
        if pinned is not None:
            return pinned, None
    return None, None


def _flatten(units: list[list[dict]]) -> list[dict]:
    return [message for unit in units for message in unit]


def _select_prefix_and_recent_tail(
    messages: list,
) -> tuple[list[dict], list[dict], object | None]:
    """Select an old contiguous prefix and keep recent tool exchanges atomic.

    The boundary preserves as much recent raw context as fits under both the
    four-exchange cap and the total tail budget. The remaining contiguous old
    prefix is summarized. The latest human instruction is returned separately
    for deterministic checkpoint pinning.
    """
    units = _history_units(messages)
    if len(units) < 2:
        pinned, _ = _latest_user_instruction(messages)
        return [], _copy_messages(messages), pinned

    pinned, pinned_message_id = _latest_user_instruction(messages)
    pinned_size = (
        estimate_size([{"role": "user", "content": pinned}])
        if pinned is not None else 0
    )
    tail_limit_chars = (
        RECENT_TAIL_MAX_TOKENS * ESTIMATED_CHARS_PER_TOKEN
    )

    # Find the earliest unit allowed in the raw suffix. The selected latest
    # user message is counted once as the pinned checkpoint section.
    tail_start = len(units) - 1
    tail_size = pinned_size
    tool_count = 0
    for index in range(len(units) - 1, -1, -1):
        unit = units[index]
        is_tool = _is_tool_exchange_unit(unit)
        unit_size = sum(
            0 if id(message) == pinned_message_id
            else estimate_size([message])
            for message in unit
        )
        if (
            index < len(units) - 1
            and (
                tail_size + unit_size > tail_limit_chars
                or (
                    is_tool
                    and tool_count >= RECENT_TOOL_RESULT_COUNT
                )
            )
        ):
            break
        if index == len(units) - 1 and tail_size + unit_size > tail_limit_chars:
            return [], _copy_messages(messages), pinned
        tail_start = index
        tail_size += unit_size
        if is_tool:
            tool_count += 1

    boundary = tail_start
    if boundary <= 0:
        return [], _copy_messages(messages), pinned

    prefix_units = units[:boundary]
    tail_units = units[boundary:]
    tail = [
        _copy_messages([message])[0]
        for unit in tail_units
        for message in unit
        if id(message) != pinned_message_id
    ]
    return _copy_messages(_flatten(prefix_units)), tail, pinned


def sanitize_context_tool_results(
    messages: list,
) -> tuple[list, int]:
    """Replace oversized result bodies in a copy while preserving protocol IDs."""
    copied = _copy_messages(messages)
    replaced = 0
    for _, _, block in collect_tool_results(copied):
        output = str(_block_field(block, "content", ""))
        token_count = estimate_context_tokens(len(output))
        if token_count <= MAX_TOOL_RESULT_TOKENS:
            continue
        placeholder = (
            "[Large tool result omitted]\n"
            f"Original result size: {token_count} estimated tokens.\n"
            "Reason: exceeded MAX_TOOL_RESULT_TOKENS."
        )
        if isinstance(block, dict):
            block["content"] = placeholder
        else:
            setattr(block, "content", placeholder)
        replaced += 1
    return copied, replaced


def write_transcript(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> Path:
    safe_messages, _ = sanitize_context_tool_results(messages)
    transcript_dir = (
        runtime.paths.transcript_dir
        if runtime is not None
        else TRANSCRIPT_DIR
    )
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{time.time_ns()}.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for message in safe_messages:
            stream.write(json.dumps(
                message,
                default=str,
                ensure_ascii=False,
            ) + "\n")
    return path


def _compact_prompt(prefix: list) -> str:
    return (
        COMPACTION_PROMPT
        + "\n\nOlder conversation history to summarize:\n"
        + json.dumps(
            prefix,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _call_compact_model(
    prompt: str,
    *,
    runtime: AgentRuntime | None,
    purpose: str,
) -> str:
    model = runtime.config.model if runtime is not None else MODEL
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    record_llm_request(
        model=model,
        max_tokens=SUMMARY_MAX_TOKENS,
        message_count=1,
        tool_count=0,
        purpose=purpose,
        agent_role="",
    )
    response = model_client.messages.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    record_llm_response(response, purpose=purpose, agent_role="")
    return extract_text(response.content)


def summarize_history(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> str:
    """Return a plain Markdown continuation checkpoint."""
    return _call_compact_model(
        _compact_prompt(messages),
        runtime=runtime,
        purpose="compact_summary",
    ).strip()


def _summarize_once(
    summary_input: list,
    runtime: AgentRuntime | None,
) -> tuple[str | None, str]:
    try:
        summary = (
            summarize_history(summary_input, runtime)
            if runtime is not None
            else summarize_history(summary_input)
        )
    except Exception as exc:
        record_event(
            "compact_summary_error",
            attempt=1,
            error_type=type(exc).__name__,
            error=str(exc)[:1000],
        )
        return None, f"{type(exc).__name__}: {exc}"
    if not str(summary or "").strip():
        return None, "summary model returned empty text"
    return str(summary).strip(), ""


def _instruction_blocks(content) -> list:
    if isinstance(content, list):
        return _copy_content(content)
    return [{"type": "text", "text": str(content)}]


def _checkpoint_message(summary: str, pinned_instruction=None) -> dict:
    checkpoint_text = f"{CONTEXT_CHECKPOINT_MARKER}\n{summary.strip()}"
    if pinned_instruction is None:
        return {"role": "user", "content": checkpoint_text}
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": checkpoint_text},
            {"type": "text", "text": LATEST_USER_INSTRUCTION_MARKER},
            *_instruction_blocks(pinned_instruction),
            {"type": "text", "text": LATEST_USER_INSTRUCTION_END_MARKER},
        ],
    }


def _merge_user_content(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left + "\n\n" + right
    left_blocks = (
        [{"type": "text", "text": left}]
        if isinstance(left, str)
        else _copy_content(left) if isinstance(left, list)
        else [{"type": "text", "text": str(left)}]
    )
    right_blocks = (
        [{"type": "text", "text": right}]
        if isinstance(right, str)
        else _copy_content(right) if isinstance(right, list)
        else [{"type": "text", "text": str(right)}]
    )
    return left_blocks + right_blocks


def _assemble_compacted_history(
    summary: str,
    tail: list,
    pinned_instruction=None,
) -> list:
    candidate = [
        _checkpoint_message(summary, pinned_instruction),
        *_copy_messages(tail),
    ]
    # Merge only the synthetic checkpoint boundary when a provider would
    # otherwise receive adjacent user roles.
    if len(candidate) >= 2 and candidate[1].get("role") == "user":
        candidate[0]["content"] = _merge_user_content(
            candidate[0]["content"],
            candidate[1].get("content", ""),
        )
        del candidate[1]
    return candidate


def _request_sizer(
    request_size_fn,
    *,
    system: str,
    tools: list | None,
):
    return request_size_fn or (
        lambda candidate: estimate_context_size(
            candidate,
            system=system,
            tools=tools,
        )
    )


def _compact_signature(messages: list, *, target: int, fixed_size: int) -> str:
    payload = json.dumps(
        {
            "messages": messages,
            "target": target,
            "fixed_size": fixed_size,
        },
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remember_failed_compact(
    runtime: AgentRuntime | None,
    *,
    reason: str,
    signature: str,
    failure: str,
) -> None:
    if runtime is None or reason != "automatic":
        return
    runtime.state.metadata["last_failed_compact_signature"] = signature
    runtime.state.metadata["last_failed_compact_reason"] = failure[:500]


def _clear_failed_compact(runtime: AgentRuntime | None) -> None:
    if runtime is None:
        return
    runtime.state.metadata.pop("last_failed_compact_signature", None)
    runtime.state.metadata.pop("last_failed_compact_reason", None)


def _record_compact(
    *,
    reason: str,
    transcript: Path,
    before_messages: int,
    before_size: int,
    after_messages: int,
    after_size: int,
    summarized_prefix: int,
    tail: list,
    summary: str,
    success: bool,
    failure_reason: str = "",
    omitted_tool_results: int = 0,
    summary_model_calls: int = 0,
) -> None:
    try:
        record_event(
            "compact",
            kind=reason,
            reason=reason,
            transcript=str(transcript),
            before_messages=before_messages,
            before_size=before_size,
            before_tokens=estimate_context_tokens(before_size),
            after_messages=after_messages,
            after_size=after_size,
            after_tokens=estimate_context_tokens(after_size),
            summarized_prefix_messages=summarized_prefix,
            recent_tail_messages=len(tail),
            recent_tail_tokens=estimate_context_tokens(estimate_size(tail)),
            summary_length=len(summary),
            success=success,
            failure_reason=failure_reason,
            oversized_result_handled=omitted_tool_results > 0,
            omitted_tool_result_count=omitted_tool_results,
            summary_model_calls=summary_model_calls,
        )
    except Exception:
        pass


def _compact(
    messages: list,
    *,
    reason: str,
    runtime: AgentRuntime | None,
    target_context_budget: int | None,
    request_size_fn,
    system: str,
    tools: list | None,
    force: bool,
    allow_model_summary: bool | None,
) -> list:
    sizer = _request_sizer(request_size_fn, system=system, tools=tools)
    original_size = sizer(messages)
    sanitized, omitted_count = sanitize_context_tool_results(messages)
    active_messages = sanitized if omitted_count else messages
    active_size = sizer(active_messages)
    transcript = write_transcript(active_messages, runtime)
    target = max(
        1_000,
        int(
            target_context_budget
            if target_context_budget is not None
            else CONTEXT_LIMIT - COMPACT_OUTPUT_RESERVE_CHARS
        ),
    )
    trigger = int(CONTEXT_LIMIT * COMPACT_TRIGGER_RATIO)
    signature = _compact_signature(
        active_messages,
        target=target,
        fixed_size=sizer([]),
    )

    if not force and active_size <= trigger:
        if omitted_count:
            _clear_failed_compact(runtime)
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=original_size,
            after_messages=len(active_messages),
            after_size=active_size,
            summarized_prefix=0,
            tail=active_messages,
            summary="",
            success=False,
            failure_reason="below compact trigger",
            omitted_tool_results=omitted_count,
        )
        return active_messages

    if (
        not force
        and reason == "automatic"
        and runtime is not None
        and runtime.state.metadata.get("last_failed_compact_signature")
        == signature
    ):
        failure = "unchanged history matches the last failed compact"
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=original_size,
            after_messages=len(active_messages),
            after_size=active_size,
            summarized_prefix=0,
            tail=active_messages,
            summary="",
            success=False,
            failure_reason=failure,
            omitted_tool_results=omitted_count,
        )
        return active_messages

    prefix, tail, pinned_instruction = (
        _select_prefix_and_recent_tail(active_messages)
    )
    if not prefix:
        failure = "no safe bounded older prefix to summarize"
        _remember_failed_compact(
            runtime,
            reason=reason,
            signature=signature,
            failure=failure,
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=original_size,
            after_messages=len(active_messages),
            after_size=active_size,
            summarized_prefix=0,
            tail=active_messages,
            summary="",
            success=False,
            failure_reason=failure,
            omitted_tool_results=omitted_count,
        )
        return active_messages

    # Reserve the maximum checkpoint output before paying for its generation.
    # If the fixed recent suffix is still too large, move whole oldest units
    # into the prefix until the candidate fits or only the final unit remains.
    while sizer(_assemble_compacted_history(
        "x" * SUMMARY_OUTPUT_RESERVE_CHARS,
        tail,
        pinned_instruction,
    )) > target:
        tail_units = _history_units(tail)
        if len(tail_units) <= 1:
            failure = (
                "minimum recent tail cannot fit with the reserved checkpoint"
            )
            _remember_failed_compact(
                runtime,
                reason=reason,
                signature=signature,
                failure=failure,
            )
            _record_compact(
                reason=reason,
                transcript=transcript,
                before_messages=len(messages),
                before_size=original_size,
                after_messages=len(active_messages),
                after_size=active_size,
                summarized_prefix=len(prefix),
                tail=tail,
                summary="",
                success=False,
                failure_reason=failure,
                omitted_tool_results=omitted_count,
            )
            return active_messages
        prefix.extend(_copy_messages(tail_units[0]))
        tail = _copy_messages(_flatten(tail_units[1:]))

    summary_prompt_budget = max(
        1_000,
        CONTEXT_LIMIT - SUMMARY_OUTPUT_RESERVE_CHARS,
    )
    if len(_compact_prompt(prefix)) > summary_prompt_budget:
        failure = "selected prefix cannot fit one safe summary request"
        _remember_failed_compact(
            runtime,
            reason=reason,
            signature=signature,
            failure=failure,
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=original_size,
            after_messages=len(active_messages),
            after_size=active_size,
            summarized_prefix=len(prefix),
            tail=tail,
            summary="",
            success=False,
            failure_reason=failure,
            omitted_tool_results=omitted_count,
        )
        return active_messages

    model_client = runtime.services.model_client if runtime is not None else client
    budget_allowed, budget = can_spend_optional_calls(model_client, 1)
    if allow_model_summary is False or not budget_allowed:
        record_event(
            "model_budget_guard",
            decision="compact_skipped",
            reason="finalization_reserve",
            estimated_calls=1,
            **{
                key: value
                for key, value in budget.items()
                if key != "available"
            },
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=original_size,
            after_messages=len(active_messages),
            after_size=active_size,
            summarized_prefix=0,
            tail=active_messages,
            summary="",
            success=False,
            failure_reason="model summary call unavailable",
            omitted_tool_results=omitted_count,
        )
        return active_messages

    summary, failure = _summarize_once(prefix, runtime)
    if not summary:
        _remember_failed_compact(
            runtime,
            reason=reason,
            signature=signature,
            failure=failure,
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=original_size,
            after_messages=len(active_messages),
            after_size=active_size,
            summarized_prefix=len(prefix),
            tail=tail,
            summary="",
            success=False,
            failure_reason=failure,
            omitted_tool_results=omitted_count,
            summary_model_calls=1,
        )
        return active_messages

    candidate = _assemble_compacted_history(
        summary,
        tail,
        pinned_instruction,
    )
    candidate_size = sizer(candidate)
    if candidate_size > target:
        failure = (
            f"assembled request remains {candidate_size} chars above target "
            f"{target}; a second summary call is forbidden"
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=original_size,
            after_messages=len(active_messages),
            after_size=active_size,
            summarized_prefix=len(prefix),
            tail=tail,
            summary=summary,
            success=False,
            failure_reason=failure,
            omitted_tool_results=omitted_count,
            summary_model_calls=1,
        )
        _remember_failed_compact(
            runtime,
            reason=reason,
            signature=signature,
            failure=failure,
        )
        return active_messages

    _record_compact(
        reason=reason,
        transcript=transcript,
        before_messages=len(messages),
        before_size=original_size,
        after_messages=len(candidate),
        after_size=candidate_size,
        summarized_prefix=len(prefix),
        tail=tail,
        summary=summary,
        success=True,
        omitted_tool_results=omitted_count,
        summary_model_calls=1,
    )
    _clear_failed_compact(runtime)
    if runtime is not None:
        runtime.state.metadata["compact_generation"] = (
            int(runtime.state.metadata.get("compact_generation", 0)) + 1
        )
    return candidate


def compact_history(
    messages: list,
    *,
    allow_model_summary: bool | None = None,
    reason: str = "automatic",
    runtime: AgentRuntime | None = None,
    target_context_budget: int | None = None,
    request_size_fn=None,
    system: str = "",
    tools: list | None = None,
) -> list:
    return _compact(
        messages,
        reason=reason or "automatic",
        runtime=runtime,
        target_context_budget=target_context_budget,
        request_size_fn=request_size_fn,
        system=system,
        tools=tools,
        force=(reason == "manual"),
        allow_model_summary=allow_model_summary,
    )


def reactive_compact(
    messages: list,
    runtime: AgentRuntime | None = None,
    *,
    target_context_budget: int | None = None,
    request_size_fn=None,
    system: str = "",
    tools: list | None = None,
) -> list:
    return _compact(
        messages,
        reason="reactive",
        runtime=runtime,
        target_context_budget=target_context_budget,
        request_size_fn=request_size_fn,
        system=system,
        tools=tools,
        force=True,
        allow_model_summary=None,
    )


import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
