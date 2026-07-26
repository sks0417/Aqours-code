from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
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
SUMMARY_OUTPUT_RESERVE_CHARS = SUMMARY_MAX_TOKENS * 3
ARCHIVE_PREVIEW_CHARS = 2_000
ARCHIVE_URI_PREFIX = "archive://"
_ARCHIVE_LOCK = threading.RLock()
_LEGACY_ARCHIVE_RUN_ID = "standalone-" + uuid.uuid4().hex[:12]

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
If a tool result is represented by an archived-result descriptor, its exact
content remains recoverable through that descriptor. Preserve the tool_use_id
when an exact detail may matter later. Do not copy an archive manifest into
the checkpoint.

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


def _collect_tool_uses(messages: list) -> dict[str, dict]:
    uses = {}
    for message in messages:
        if not message_has_tool_use(message):
            continue
        for block in message["content"]:
            if block_type(block) != "tool_use":
                continue
            tool_use_id = str(_block_field(block, "id", ""))
            if not tool_use_id:
                continue
            tool_input = _block_field(block, "input", {})
            uses[tool_use_id] = {
                "tool_name": str(_block_field(block, "name", "")),
                "tool_input": (
                    deepcopy(tool_input)
                    if isinstance(tool_input, dict)
                    else {}
                ),
            }
    return uses


def _archive_location(
    runtime: AgentRuntime | None,
    *,
    create: bool,
) -> tuple[str, Path]:
    recorder = (
        runtime.services.trace_recorder
        if runtime is not None
        else None
    )
    if recorder is None:
        getter = globals().get("get_current_run")
        recorder = getter() if callable(getter) else None
    if recorder is not None and getattr(recorder, "run_dir", None):
        run_id = str(recorder.run_id)
        root = Path(recorder.run_dir) / "artifacts" / "compacted-tool-results"
    elif runtime is not None:
        run_id = str(runtime.state.metadata.setdefault(
            "archive_run_id",
            "runtime-" + uuid.uuid4().hex[:12],
        ))
        root = (
            runtime.paths.state_root
            / ".codepilot"
            / "runs"
            / run_id
            / "artifacts"
            / "compacted-tool-results"
        )
    else:
        run_id = _LEGACY_ARCHIVE_RUN_ID
        root = (
            Path(WORKDIR)
            / ".codepilot"
            / "runs"
            / run_id
            / "artifacts"
            / "compacted-tool-results"
        )
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return run_id, root


def _read_manifest(manifest_path: Path) -> list[dict]:
    records = []
    try:
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    except (OSError, json.JSONDecodeError):
        return records
    return records


def _archive_prefix_tool_results(
    prefix: list,
    runtime: AgentRuntime | None,
) -> tuple[list[dict], str]:
    """Archive every exact Tool Result that is about to leave live context."""
    results = collect_tool_results(prefix)
    if not results:
        return [], ""
    run_id, archive_root = _archive_location(runtime, create=True)
    manifest_path = archive_root / "manifest.jsonl"
    tool_uses = _collect_tool_uses(prefix)
    archived = []
    with _ARCHIVE_LOCK:
        existing = _read_manifest(manifest_path)
        by_key = {
            (str(item.get("tool_use_id", "")), str(item.get("sha256", ""))): item
            for item in existing
        }
        appended = []
        for _, _, block in results:
            tool_use_id = str(_block_field(block, "tool_use_id", "unknown"))
            output = str(_block_field(block, "content", ""))
            digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
            existing_record = by_key.get((tool_use_id, digest))
            if existing_record is not None:
                output_path = Path(str(existing_record.get("output_path", "")))
                try:
                    reusable = (
                        output_path.resolve().parent == archive_root.resolve()
                        and output_path.is_file()
                        and not output_path.is_symlink()
                        and hashlib.sha256(output_path.read_bytes()).hexdigest()
                        == digest
                    )
                except OSError:
                    reusable = False
                if reusable:
                    archived.append(existing_record)
                    continue

            filename = _safe_result_filename(tool_use_id)
            output_path = archive_root / filename
            if output_path.exists():
                output_path = archive_root / (
                    output_path.stem + "-" + digest[:12] + ".txt"
                )
            output_path.write_text(output, encoding="utf-8")
            use = tool_uses.get(tool_use_id, {})
            record = {
                "tool_use_id": tool_use_id,
                "tool_name": str(use.get("tool_name", "")),
                "tool_input": dict(use.get("tool_input", {})),
                "output_path": str(output_path),
                "archive_id": f"{run_id}/{output_path.name}",
                "character_count": len(output),
                "sha256": digest,
            }
            by_key[(tool_use_id, digest)] = record
            appended.append(record)
            archived.append(record)
        if appended:
            with manifest_path.open("a", encoding="utf-8") as stream:
                for record in appended:
                    stream.write(json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n")
    return archived, f"{ARCHIVE_URI_PREFIX}{run_id}/manifest.jsonl"


def _archived_descriptor(record: dict, output: str) -> str:
    preview_chars = min(ARCHIVE_PREVIEW_CHARS, max(0, len(output)))
    head_chars = preview_chars // 2
    tail_chars = preview_chars - head_chars
    head = output[:head_chars]
    tail = output[-tail_chars:] if len(output) > preview_chars else ""
    return "\n".join([
        "<archived-tool-result>",
        f"tool_use_id: {record.get('tool_use_id', '')}",
        f"tool_name: {record.get('tool_name', '')}",
        "tool_input: " + json.dumps(
            record.get("tool_input", {}),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        f"archive_id: {record.get('archive_id', '')}",
        f"output_path: {record.get('output_path', '')}",
        f"character_count: {record.get('character_count', len(output))}",
        f"sha256: {record.get('sha256', '')}",
        "First output:",
        head,
        "Last output:",
        tail,
        "</archived-tool-result>",
    ])


def read_archived_tool_result(
    tool_use_id: str,
    offset: int | None = None,
    limit: int | None = None,
    runtime: AgentRuntime | None = None,
) -> str:
    """Read one exact result from the current run archive by Tool-use ID."""
    try:
        _, archive_root = _archive_location(runtime, create=False)
        records = _read_manifest(archive_root / "manifest.jsonl")
        record = next(
            (
                item
                for item in reversed(records)
                if str(item.get("tool_use_id", "")) == str(tool_use_id)
            ),
            None,
        )
        if record is None:
            return f"Error: archived tool result not found: {tool_use_id}"
        output_path = Path(str(record.get("output_path", "")))
        resolved = output_path.resolve()
        if (
            resolved.parent != archive_root.resolve()
            or not resolved.is_file()
            or resolved.is_symlink()
        ):
            return "Error: invalid archived tool result path"
        output = resolved.read_text(encoding="utf-8")
        expected_digest = str(record.get("sha256", ""))
        if (
            expected_digest
            and hashlib.sha256(output.encode("utf-8")).hexdigest()
            != expected_digest
        ):
            return "Error: archived tool result digest mismatch"
        if offset is None and limit is None:
            return output
        lines = output.splitlines()
        start = max(0, int(offset or 0))
        selected = lines[start:]
        if limit is not None:
            selected = selected[:max(0, int(limit))]
        return "\n".join(selected)
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


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


def _prepare_summary_input(
    prefix: list,
    archived: list[dict],
    *,
    max_prompt_chars: int,
) -> tuple[list | None, int]:
    """Fit one summary request by masking archived result copies only."""
    summary_input = deepcopy(prefix)
    if len(_compact_prompt(summary_input)) <= max_prompt_chars:
        return summary_input, 0
    records = {
        (str(record.get("tool_use_id", "")), str(record.get("sha256", ""))):
        record
        for record in archived
    }
    candidates = []
    for _, _, block in collect_tool_results(summary_input):
        output = str(_block_field(block, "content", ""))
        tool_use_id = str(_block_field(block, "tool_use_id", ""))
        digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
        record = records.get((tool_use_id, digest))
        if record is not None:
            candidates.append((len(output), block, output, record))
    masked = 0
    for _, block, output, record in sorted(candidates, reverse=True, key=lambda x: x[0]):
        descriptor = _archived_descriptor(record, output)
        if isinstance(block, dict):
            block["content"] = descriptor
        else:
            setattr(block, "content", descriptor)
        masked += 1
        if len(_compact_prompt(summary_input)) <= max_prompt_chars:
            return summary_input, masked
    return None, masked


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


def _append_archive_locator(summary: str, manifest_uri: str) -> str:
    if not manifest_uri:
        return summary.strip()
    return (
        summary.strip()
        + "\n\n## Archived tool results\n\n"
        + f"Manifest: `{manifest_uri}`\n\n"
        + "Exact outputs removed from the live context can be recovered from "
        + "this manifest by tool_use_id, tool name, or original tool input. "
        + "Use `read_archived_tool_result` and reuse archived results instead "
        + "of rerunning unchanged tools."
    )


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
    archived_results: int = 0,
    masked_summary_results: int = 0,
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
            oversized_result_handled=masked_summary_results > 0,
            archived_result_count=archived_results,
            masked_summary_result_count=masked_summary_results,
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

    fixed_request_size = sizer([])
    usable_message_chars = target - fixed_request_size
    locator_reserve = 800
    tail_capacity = (
        usable_message_chars
        - SUMMARY_OUTPUT_RESERVE_CHARS
        - locator_reserve
    )
    if tail_capacity < 256:
        failure = (
            "fixed request plus summary output reserve leaves no safe recent "
            "tail budget"
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
            failure_reason=failure,
        )
        return messages
    tail_budget = min(
        _recent_tail_budget_chars(target),
        tail_capacity,
    )
    prefix, tail = _select_prefix_and_recent_tail(
        messages,
        tail_budget_chars=tail_budget,
    )
    if not prefix:
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
            failure_reason="no safe older prefix to summarize",
        )
        return messages

    reserved_candidate = _assemble_compacted_history(
        "x" * (SUMMARY_OUTPUT_RESERVE_CHARS + locator_reserve),
        tail,
    )
    if sizer(reserved_candidate) > target:
        failure = (
            "complete recent tail cannot fit with the reserved checkpoint; "
            "tail Tool Results remain verbatim"
        )
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
        )
        return messages

    archived, manifest_uri = _archive_prefix_tool_results(prefix, runtime)
    summary_prompt_budget = max(
        1_000,
        CONTEXT_LIMIT - SUMMARY_OUTPUT_RESERVE_CHARS,
    )
    summary_input, masked_count = _prepare_summary_input(
        prefix,
        archived,
        max_prompt_chars=summary_prompt_budget,
    )
    if summary_input is None:
        failure = (
            "safe summary request cannot fit without deleting pinned "
            "checkpoint or user instructions"
        )
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
            archived_results=len(archived),
            masked_summary_results=masked_count,
        )
        return messages

    summary, failure = _summarize_once(summary_input, runtime)
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
            archived_results=len(archived),
            masked_summary_results=masked_count,
            summary_model_calls=1,
        )
        return messages

    checkpoint = _append_archive_locator(summary, manifest_uri)
    candidate = _assemble_compacted_history(checkpoint, tail)
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
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=len(prefix),
            tail=tail,
            summary=checkpoint,
            success=False,
            failure_reason=failure,
            archived_results=len(archived),
            masked_summary_results=masked_count,
            summary_model_calls=1,
        )
        return messages

    _record_compact(
        reason=reason,
        transcript=transcript,
        before_messages=len(messages),
        before_size=before_size,
        after_messages=len(candidate),
        after_size=candidate_size,
        summarized_prefix=len(prefix),
        tail=tail,
        summary=checkpoint,
        success=True,
        archived_results=len(archived),
        masked_summary_results=masked_count,
        summary_model_calls=1,
    )
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
