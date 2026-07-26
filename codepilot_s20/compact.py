from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from pathlib import Path

from .model_budget import can_spend_optional_calls
from .runtime import AgentRuntime
from .runtime_state import *


# A compacted conversation has only two durable pieces: one cumulative Markdown
# checkpoint and a small verbatim suffix. Current runtime facts are rebuilt by
# context.py and never inferred from this checkpoint.
CONTEXT_CHECKPOINT_MARKER = "[Context checkpoint]"
COMPACT_TRIGGER_RATIO = 0.85
RECENT_TAIL_MAX_TOKENS = 8_000
RECENT_TAIL_RATIO = 0.15
COMPACT_OUTPUT_RESERVE_CHARS = 6_000
SUMMARY_MAX_TOKENS = 2_000
SUMMARY_OVERFLOW_RETRIES = 2
POST_COMPACT_REASSEMBLY_LIMIT = 1

COMPACTION_PROMPT = """\
You are creating a context checkpoint for another coding-agent model
that will continue the current task.

Summarize the older conversation history into a concise continuation handoff.
The newest messages may remain available verbatim outside this summary, so
focus on older information that is still needed to continue correctly.

Preserve:
- the user's current goal and hard constraints
- progress already made and the current focus
- important decisions and why they were made
- concrete information learned from important tool results
- relevant files, file responsibilities, symbols, code behavior and relationships
- changes already performed
- commands and tests run, including their outcomes
- exact error messages and failed approaches when still relevant
- unresolved problems and immediate next steps

For tool results, preserve what was learned from them. Do not merely state
that a command was executed or that a file was read.

Use exact file paths, symbol names, commands, errors, URLs and identifiers
when they matter. Give more detail to recent and currently relevant work.
Remove obsolete, duplicated, conversational and low-value details.

If an earlier context checkpoint is present, update it: preserve facts that
are still true, remove stale facts, and merge in newer information.

Do not answer the original task. Do not describe the summarization process.
Do not invent facts. Return concise Markdown only.
"""


class ContextCompactionError(RuntimeError):
    """Raised when a successful summary still cannot fit the request budget."""


def estimate_size(messages: list) -> int:
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
    """Conservatively estimate every assembled request component."""
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
    # The core supports multiple providers and has no common tokenizer. Three
    # characters per token is deliberately conservative for mixed code/CJK.
    return (max(0, int(size_chars)) + 2) // 3


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
        return bool(content.strip())
    if not isinstance(content, list):
        return bool(str(content).strip())
    return any(
        block_type(block) == "text"
        and str(_block_field(block, "text", "")).strip()
        for block in content
    )


def _flatten(units: list[list[dict]]) -> list[dict]:
    return [message for unit in units for message in unit]


def _recent_tail_budget_chars(usable_context_chars: int) -> int:
    usable_tokens = estimate_context_tokens(usable_context_chars)
    tail_tokens = min(
        RECENT_TAIL_MAX_TOKENS,
        max(1, int(usable_tokens * RECENT_TAIL_RATIO)),
    )
    return tail_tokens * 3


def _select_prefix_and_recent_tail(
    messages: list,
    *,
    tail_budget_chars: int,
) -> tuple[list[dict], list[dict]]:
    """Select a contiguous recent suffix while keeping tool pairs atomic."""
    units = _history_units(messages)
    if not units:
        return [], []

    start = len(units)
    used = 0
    for index in range(len(units) - 1, -1, -1):
        unit = units[index]
        # Prior checkpoints must be folded into the next checkpoint rather than
        # accumulating in the raw tail.
        if any(_is_checkpoint_message(message) for message in unit):
            break
        unit_size = estimate_size(unit)
        if start < len(units) and used + unit_size > tail_budget_chars:
            break
        start = index
        used += unit_size

    if start == len(units):
        start = len(units) - 1

    prefix_units = units[:start]
    tail_units = units[start:]
    tail = deepcopy(_flatten(tail_units))

    # Preserve the latest human instruction verbatim even when a long tool run
    # pushed it outside the bounded suffix. The original copy remains in the
    # summarized prefix, so chronological evidence is not rewritten.
    tail_message_ids = {id(message) for unit in tail_units for message in unit}
    latest_user = next(
        (
            message
            for message in reversed(messages)
            if _is_user_instruction(message)
        ),
        None,
    )
    if latest_user is not None and id(latest_user) not in tail_message_ids:
        tail.insert(0, deepcopy(latest_user))

    return deepcopy(_flatten(prefix_units)), tail


def _safe_result_filename(tool_use_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tool_use_id or "unknown"))
    return (safe[:120] or "unknown") + ".txt"


def persist_large_output(
    tool_use_id: str,
    output: str,
    *,
    force: bool = False,
    preview_chars: int | None = None,
    runtime: AgentRuntime | None = None,
) -> str:
    """Persist one exceptional result and return a locatable head/tail preview."""
    if not force and len(output) <= PERSIST_THRESHOLD:
        return output
    results_dir = (
        runtime.paths.tool_results_dir
        if runtime is not None
        else TOOL_RESULTS_DIR
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / _safe_result_filename(tool_use_id)
    path.write_text(output, encoding="utf-8")
    lines = output.splitlines()
    preview_chars = (
        PERSIST_PREVIEW_CHARS
        if preview_chars is None
        else max(0, int(preview_chars))
    )
    head_chars = preview_chars // 2
    tail_chars = preview_chars - head_chars
    head = output[:head_chars]
    tail = output[-tail_chars:] if tail_chars and len(output) > preview_chars else ""
    parts = [
        "<persisted-output>",
        f"Source tool result: {tool_use_id}",
        f"Full output: {path}",
        f"Character count: {len(output)}",
        f"Line count: {len(lines)}",
    ]
    if head:
        parts.extend(("First output:", head))
    if tail:
        parts.extend(("Last output:", tail))
    parts.append("</persisted-output>")
    return "\n".join(parts)


def _bound_oversized_tool_results(
    messages: list,
    runtime: AgentRuntime | None,
) -> tuple[list, int]:
    bounded = deepcopy(messages)
    changed = 0
    for _, _, block in collect_tool_results(bounded):
        content = str(_block_field(block, "content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue
        tool_use_id = str(_block_field(block, "tool_use_id", "unknown"))
        replacement = persist_large_output(
            tool_use_id,
            content,
            runtime=runtime,
        )
        if isinstance(block, dict):
            block["content"] = replacement
        else:
            setattr(block, "content", replacement)
        changed += 1
    return bounded, changed


def write_transcript(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> Path:
    transcript_dir = (
        runtime.paths.transcript_dir
        if runtime is not None
        else TRANSCRIPT_DIR
    )
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{time.time_ns()}.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for message in messages:
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


def _is_context_overflow(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return (
        ("prompt" in text and "long" in text)
        or "context_length_exceeded" in text
        or "max_context_window" in text
        or ("context" in text and "overflow" in text)
    )


def _summarize_with_overflow_retry(
    prefix: list,
    runtime: AgentRuntime | None,
) -> tuple[str | None, int, str]:
    units = _history_units(prefix)
    dropped = 0
    for attempt in range(SUMMARY_OVERFLOW_RETRIES + 1):
        current = _flatten(units)
        if not current:
            return None, dropped, "summary prefix became empty"
        try:
            summary = (
                summarize_history(current, runtime)
                if runtime is not None
                else summarize_history(current)
            )
        except Exception as exc:
            record_event(
                "compact_summary_error",
                attempt=attempt + 1,
                error_type=type(exc).__name__,
                error=str(exc)[:1000],
                context_overflow=_is_context_overflow(exc),
            )
            if not _is_context_overflow(exc):
                return None, dropped, f"{type(exc).__name__}: {exc}"
            if attempt >= SUMMARY_OVERFLOW_RETRIES or len(units) <= 1:
                return None, dropped, "summary request context overflow"
            units.pop(0)
            dropped += 1
            continue
        if not str(summary or "").strip():
            return None, dropped, "summary model returned empty text"
        return str(summary).strip(), dropped, ""
    return None, dropped, "summary retry limit reached"


def _checkpoint_message(summary: str) -> dict:
    return {
        "role": "user",
        "content": f"{CONTEXT_CHECKPOINT_MARKER}\n{summary.strip()}",
    }


def _merge_user_content(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left + "\n\n" + right
    left_blocks = (
        [{"type": "text", "text": left}]
        if isinstance(left, str)
        else deepcopy(left) if isinstance(left, list)
        else [{"type": "text", "text": str(left)}]
    )
    right_blocks = (
        [{"type": "text", "text": right}]
        if isinstance(right, str)
        else deepcopy(right) if isinstance(right, list)
        else [{"type": "text", "text": str(right)}]
    )
    return left_blocks + right_blocks


def _assemble_compacted_history(summary: str, tail: list) -> list:
    candidate = [_checkpoint_message(summary), *deepcopy(tail)]
    # Only the checkpoint boundary is synthetic. Merge it with an adjacent user
    # message so providers that require alternating roles receive valid history.
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
    oversized_results: int = 0,
    dropped_prefix_units: int = 0,
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
            oversized_result_handled=oversized_results > 0,
            oversized_result_count=oversized_results,
            dropped_prefix_units=dropped_prefix_units,
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
    transcript = write_transcript(messages, runtime)
    sizer = _request_sizer(request_size_fn, system=system, tools=tools)
    before_size = sizer(messages)
    target = max(
        1_000,
        int(
            target_context_budget
            if target_context_budget is not None
            else CONTEXT_LIMIT - COMPACT_OUTPUT_RESERVE_CHARS
        ),
    )
    trigger = int(CONTEXT_LIMIT * COMPACT_TRIGGER_RATIO)

    if not force and before_size <= trigger:
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=0,
            tail=messages,
            summary="",
            success=False,
            failure_reason="below compact trigger",
        )
        return messages

    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    if allow_model_summary is None:
        allow_model_summary, budget = can_spend_optional_calls(model_client, 1)
    else:
        budget = {}
    if not allow_model_summary:
        record_event(
            "model_budget_guard",
            decision="compact_skipped",
            reason="finalization_reserve",
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
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=0,
            tail=messages,
            summary="",
            success=False,
            failure_reason="model summary call unavailable",
        )
        return messages

    bounded, oversized_count = _bound_oversized_tool_results(messages, runtime)
    fixed_request_size = sizer([])
    usable_message_chars = max(1_000, target - fixed_request_size)
    initial_tail_budget = min(
        _recent_tail_budget_chars(target),
        max(1_000, usable_message_chars - COMPACT_OUTPUT_RESERVE_CHARS),
    )
    tail_budget = initial_tail_budget
    last_candidate: list | None = None
    last_size = before_size

    for reassembly in range(POST_COMPACT_REASSEMBLY_LIMIT + 1):
        prefix, tail = _select_prefix_and_recent_tail(
            bounded,
            tail_budget_chars=tail_budget,
        )
        if not prefix:
            processed_size = sizer(bounded)
            if oversized_count and processed_size <= target:
                _record_compact(
                    reason=reason,
                    transcript=transcript,
                    before_messages=len(messages),
                    before_size=before_size,
                    after_messages=len(bounded),
                    after_size=processed_size,
                    summarized_prefix=0,
                    tail=bounded,
                    summary="",
                    success=True,
                    oversized_results=oversized_count,
                )
                if runtime is not None:
                    runtime.state.metadata["compact_generation"] = (
                        int(runtime.state.metadata.get("compact_generation", 0))
                        + 1
                    )
                return bounded
            _record_compact(
                reason=reason,
                transcript=transcript,
                before_messages=len(messages),
                before_size=before_size,
                after_messages=len(messages),
                after_size=before_size,
                summarized_prefix=0,
                tail=bounded,
                summary="",
                success=False,
                failure_reason="no safe older prefix to summarize",
                oversized_results=oversized_count,
            )
            return messages

        summary, dropped, failure = _summarize_with_overflow_retry(
            prefix,
            runtime,
        )
        if not summary:
            _record_compact(
                reason=reason,
                transcript=transcript,
                before_messages=len(messages),
                before_size=before_size,
                after_messages=len(messages),
                after_size=before_size,
                summarized_prefix=len(prefix),
                tail=tail,
                summary="",
                success=False,
                failure_reason=failure,
                oversized_results=oversized_count,
                dropped_prefix_units=dropped,
            )
            return messages

        candidate = _assemble_compacted_history(summary, tail)
        candidate_size = sizer(candidate)
        last_candidate = candidate
        last_size = candidate_size
        if candidate_size <= target:
            _record_compact(
                reason=reason,
                transcript=transcript,
                before_messages=len(messages),
                before_size=before_size,
                after_messages=len(candidate),
                after_size=candidate_size,
                summarized_prefix=len(prefix),
                tail=tail,
                summary=summary,
                success=True,
                oversized_results=oversized_count,
                dropped_prefix_units=dropped,
            )
            if runtime is not None:
                runtime.state.metadata["compact_generation"] = (
                    int(runtime.state.metadata.get("compact_generation", 0)) + 1
                )
            return candidate

        # Re-select a smaller suffix and summarize the now-larger prefix once.
        # This is a bounded postcondition retry, not incremental semantic merge.
        tail_budget = max(1_000, tail_budget // 2)

    failure = (
        f"assembled request remains {last_size} chars, above target {target}, "
        f"after oversized-result handling and one tail reduction"
    )
    _record_compact(
        reason=reason,
        transcript=transcript,
        before_messages=len(messages),
        before_size=before_size,
        after_messages=len(last_candidate or messages),
        after_size=last_size,
        summarized_prefix=0,
        tail=[],
        summary="",
        success=False,
        failure_reason=failure,
        oversized_results=oversized_count,
    )
    raise ContextCompactionError(failure)


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
