import json
import re
import time
from copy import deepcopy
from pathlib import Path

from .runtime_state import *
from .model_budget import can_spend_optional_calls
from .knowledge import normalize_knowledge_path
from .runtime import AgentRuntime

# ── Context Compaction ──

# Compaction is layered: first make only provenance-preserving reductions,
# then extract semantics from complete outgoing exchanges, and only then
# remove their raw messages.
def estimate_size(messages: list) -> int:
    return len(json.dumps(messages, default=str))


def estimate_context_size(
    messages: list,
    *,
    system: str = "",
    tools: list | None = None,
    dynamic: dict | None = None,
) -> int:
    """Conservative shared estimate for every request component."""
    payload = {
        "system": system,
        "messages": messages,
        "tools": tools or [],
        "dynamic": dynamic or {},
    }
    return len(json.dumps(
        payload, default=str, ensure_ascii=False, separators=(",", ":"),
    ))


def estimate_context_tokens(size_chars: int) -> int:
    # No provider tokenizer is available in the core runtime. Three characters
    # per token is deliberately conservative for mixed code/CJK/JSON prompts.
    return (max(0, int(size_chars)) + 2) // 3

def block_type(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def message_has_tool_use(message: dict) -> bool:
    if message.get("role") != "assistant":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(block_type(block) == "tool_use" for block in content)


def is_tool_result_message(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)


def collect_tool_results(messages: list):
    found = []
    for mi, msg in enumerate(messages):
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append((mi, bi, block))
    return found


def collect_tool_result_messages(messages: list):
    found = []
    for mi, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        blocks = [block for block in content
                  if isinstance(block, dict)
                  and block.get("type") == "tool_result"]
        if blocks:
            found.append((mi, message, blocks))
    return found


def _block_field(block, name: str, default=None):
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _collect_tool_uses(messages: list) -> dict[str, dict]:
    uses = {}
    for message in messages:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if block_type(block) != "tool_use":
                continue
            tool_use_id = _block_field(block, "id")
            if not tool_use_id:
                continue
            tool_input = _block_field(block, "input", {})
            uses[str(tool_use_id)] = {
                "name": str(_block_field(block, "name", "")),
                "input": tool_input if isinstance(tool_input, dict) else {},
            }
    return uses


def _normalized_read_path(tool_use: dict) -> str:
    return normalize_knowledge_path(
        str(tool_use.get("input", {}).get("path", "")),
    )


def _compact_duplicate_read_results(messages: list, batches: list,
                                    tool_uses: dict, target_size: int):
    """Compact older identical reads first while retaining the newest copy."""
    seen: dict[tuple[str, str], str] = {}
    duplicates = []
    for _, _, blocks in reversed(batches):
        for block in reversed(blocks):
            tool_use_id = str(block.get("tool_use_id", ""))
            tool_use = tool_uses.get(tool_use_id)
            if not tool_use or tool_use["name"] != "read_file":
                continue
            path = _normalized_read_path(tool_use)
            content = str(block.get("content", ""))
            if not path or len(content) <= 120:
                continue
            key = (path, content)
            newer_tool_use_id = seen.get(key)
            if newer_tool_use_id is None:
                seen[key] = tool_use_id
                continue
            duplicates.append((block, newer_tool_use_id))

    for block, newer_tool_use_id in reversed(duplicates):
        block["content"] = (
            "[Duplicate read compacted. Identical content is retained in "
            f"newer tool result {newer_tool_use_id}.]"
        )
        if estimate_size(messages) <= target_size:
            break


def persist_large_output(tool_use_id: str, output: str, *, force: bool = False,
                         preview_chars: int | None = None,
                         runtime: AgentRuntime | None = None) -> str:
    if not force and len(output) <= PERSIST_THRESHOLD:
        return output
    results_dir = (
        runtime.paths.tool_results_dir if runtime is not None
        else TOOL_RESULTS_DIR
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    lines = output.splitlines()
    preview_chars = (PERSIST_PREVIEW_CHARS if preview_chars is None
                     else max(0, int(preview_chars)))
    head_chars = preview_chars // 2
    tail_chars = preview_chars - head_chars
    head = output[:head_chars] if head_chars else ""
    tail = (output[-tail_chars:]
            if tail_chars and len(output) > preview_chars else "")
    parts = [
        "<persisted-output>",
        f"Source tool result: {tool_use_id}",
        f"Full output: {path}",
        f"Character count: {len(output)}",
        f"Line count: {len(lines)}",
    ]
    if head:
        parts.extend(["First output:", head])
    if tail:
        parts.extend(["Last output:", tail])
    parts.append("</persisted-output>")
    return "\n".join(parts)


def tool_result_budget(
    messages: list,
    max_bytes: int | None = None,
    runtime: AgentRuntime | None = None,
) -> list:
    if not messages:
        return messages
    max_bytes = TOOL_RESULT_BATCH_LIMIT if max_bytes is None else int(max_bytes)
    batches = collect_tool_result_messages(messages)
    if not batches:
        return messages
    # Persist every oversized result for provenance, but keep its complete
    # in-memory content until semantic extraction succeeds. A persisted path
    # and head/tail preview are not semantic preservation.
    for _, _, batch_blocks in batches:
        for block in batch_blocks:
            text = str(block.get("content", ""))
            if len(text) > PERSIST_THRESHOLD:
                persist_large_output(
                    block.get("tool_use_id", "unknown"),
                    text,
                    runtime=runtime,
                )
    return messages


def snip_compact(messages: list, max_messages: int | None = None,
                 trigger_size: int | None = None) -> list:
    # Semantic deletion belongs to full compact, where the outgoing history is
    # first converted into canonical SessionSemanticMemory. Keeping this
    # compatibility entry point as a no-op prevents callers from silently
    # dropping middle messages before that extraction.
    return messages


def micro_compact(messages: list, trigger_size: int | None = None,
                  target_size: int | None = None,
                  runtime: AgentRuntime | None = None) -> list:
    trigger_size = (MICRO_COMPACT_TRIGGER if trigger_size is None
                    else int(trigger_size))
    target_size = (MICRO_COMPACT_TARGET if target_size is None
                   else int(target_size))
    if estimate_size(messages) <= trigger_size:
        return messages
    batches = collect_tool_result_messages(messages)
    if not batches:
        return messages
    tool_uses = _collect_tool_uses(messages)

    # Repeated reads are pure duplication. Reclaim them before sacrificing a
    # different file or command result, including when both reads are recent.
    _compact_duplicate_read_results(
        messages, batches, tool_uses, target_size)
    # Unique results must remain intact until full compact has extracted their
    # semantics. In particular, the number of RunKnowledge files must never
    # expand a protected raw-result working set.
    return messages


def write_transcript(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> Path:
    transcript_dir = (
        runtime.paths.transcript_dir if runtime is not None
        else TRANSCRIPT_DIR
    )
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path


COMPACT_INPUT_LIMIT = 80000
COMPACT_HISTORY_CHUNK_LIMIT = 60000
CHECKPOINT_LIMIT = 1800
COMPACT_OUTPUT_RESERVE_CHARS = 6000
MAX_COMPACT_PASSES = 16


class ContextCompactionError(RuntimeError):
    """Raised when Compact cannot safely satisfy its request budget."""


def _compact_output_schema() -> dict:
    return {
        "conversation_checkpoint": {
            "summary": "string",
            "current_focus": "string",
            "remaining": ["string"],
        },
        "semantic_memory_delta": {
            "task": {
                "goal": "string",
                "constraints": ["string"],
                "definition_of_done": ["string"],
            },
            "progress": {
                "completed": ["string"],
                "current_focus": "string",
                "remaining": ["string"],
            },
            "files": [{
                "path": "normalized path",
                "digest": "ignored model hint; runtime supplies authority",
                "stale": False,
                "source_tool_use_ids": ["read_file tool_use id"],
                "supersede_fields": [
                    "semantic field names whose prior value is replaced"
                ],
                "purpose": "string",
                "key_symbols": ["string"],
                "key_behaviors": ["string"],
                "important_conditions": ["string"],
                "relationships": ["string"],
                "relevant_ranges": ["string"],
                "short_snippets": ["short exact text"],
                "conclusions": ["confirmed or model-understood conclusion"],
                "uncertainties": ["unconfirmed inference"],
            }],
            "decisions": ["string"],
            "rejected_approaches": ["string"],
            "failures": ["string"],
            "open_questions": ["string"],
            "next_actions": ["string"],
            "processed_tool_use_ids": ["every tool result semantically processed"],
        },
    }


def _parse_json_object(text: str) -> dict | None:
    raw = str(text or "").strip()
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S | re.I)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _validate_compact_payload(value: dict | None) -> dict | None:
    if not isinstance(value, dict) or set(value) != {
        "conversation_checkpoint", "semantic_memory_delta",
    }:
        return None
    checkpoint = value["conversation_checkpoint"]
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != {"summary", "current_focus", "remaining"}
    ):
        return None
    if not isinstance(checkpoint.get("summary", ""), str):
        return None
    if not isinstance(checkpoint.get("current_focus", ""), str):
        return None
    remaining = checkpoint.get("remaining", [])
    if not isinstance(remaining, list):
        return None
    from .semantic_memory import validate_semantic_delta
    delta = validate_semantic_delta(value["semantic_memory_delta"])
    if delta is None:
        return None
    return {
        "conversation_checkpoint": {
            "summary": checkpoint.get("summary", "")[:1200],
            "current_focus": checkpoint.get("current_focus", "")[:400],
            "remaining": [
                str(item)[:300] for item in remaining[:8]
            ],
        },
        "semantic_memory_delta": delta,
        "_transfer_complete": True,
    }


def _history_units(messages: list) -> list[list[dict]]:
    units = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if (
            message_has_tool_use(message)
            and index + 1 < len(messages)
            and is_tool_result_message(messages[index + 1])
        ):
            units.append([message, messages[index + 1]])
            index += 2
        else:
            units.append([message])
            index += 1
    return units


def _is_prior_checkpoint(unit: list[dict]) -> bool:
    if len(unit) != 1:
        return False
    content = unit[0].get("content")
    return (
        isinstance(content, str)
        and content.startswith(("[Compacted checkpoint]", "[Reactive checkpoint]"))
    )


def _history_observations(
    messages: list,
    runtime: AgentRuntime | None,
) -> dict[str, dict]:
    if runtime is None:
        return {}
    tool_ids = set(_collect_tool_uses(messages))
    observations = runtime.state.read_observations
    return {
        tool_use_id: {
            "path": observation.path,
            "digest": observation.digest,
            "offset": observation.offset,
            "limit": observation.limit,
            "range_start": observation.range_start,
            "range_end": observation.range_end,
            "total_lines": observation.total_lines,
        }
        for tool_use_id, observation in observations.items()
        if tool_use_id in tool_ids
    }


def _current_file_digests(runtime: AgentRuntime | None) -> dict[str, str]:
    if runtime is None:
        return {}
    return {
        path: record.digest
        for path, record in runtime.state.knowledge.files.items()
        if record.digest
    }


def _compact_prompt(
    messages: list,
    runtime: AgentRuntime | None,
) -> str:
    root_task = str(
        runtime.state.root_task if runtime is not None
        else CURRENT_ROOT_TASK or ""
    )[:6000]
    observations = _history_observations(messages, runtime)
    current_state = (
        runtime.state.semantic_memory.as_dict()
        if runtime is not None else {}
    )
    return (
        "Convert only the outgoing raw conversation history below into a "
        "continuation checkpoint and a semantic-memory delta. Return JSON only "
        "and exactly match the supplied shape. Preserve the user's original "
        "goal and hard constraints; completed work and current progress; each "
        "important file's responsibility and behavior; cross-file calls/data "
        "relationships; confirmed conclusions separately from uncertainties; "
        "modifications; tests and failures; decisions and rejected approaches; "
        "open questions and next actions. Preserve only a few short exact "
        "snippets when reliable paraphrase would lose essential code or wording. "
        "Do not claim semantic memory is verified proof. Do not summarize a "
        "prior compact checkpoint. For every file card, cite the read_file "
        "tool_use id(s) that supplied its content. The runtime—not this output—"
        "owns digest and stale; model values for those fields are ignored. "
        "The original goal is immutable: copy it only for readability and do "
        "not attempt to revise it. progress.remaining, open_questions, and "
        "next_actions are complete current snapshots after applying this "
        "history to Current semantic state. completed entries must not remain "
        "in remaining. Use supersede_fields when a newer observation corrects "
        "a semantic field on the same file version. decisions, failures, and "
        "rejected approaches are bounded historical additions.\n\n"
        "processed_tool_use_ids must list every tool_use id whose result appears "
        "in this outgoing batch, but only after its content has been represented "
        "in the checkpoint or semantic delta.\n\n"
        f"Original task:\n{root_task}\n\n"
        "Current semantic state (reference for convergent updates; do not "
        "summarize it again):\n"
        + json.dumps(
            current_state,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\nRead observations captured at tool execution time:\n"
        + json.dumps(
            observations,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\nRequired JSON shape:\n"
        + json.dumps(
            _compact_output_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\nOutgoing raw history:\n"
        + json.dumps(
            messages, default=str, ensure_ascii=False, separators=(",", ":"),
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
        model=model, max_tokens=2000, message_count=1, tool_count=0,
        purpose=purpose, agent_role="",
    )
    try:
        response = model_client.messages.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000)
    except Exception as exc:
        record_event(
            "compact_summary_error", error_type=type(exc).__name__,
            error=str(exc)[:1000],
        )
        raise
    record_llm_response(response, purpose=purpose, agent_role="")
    return extract_text(response.content)


def _fallback_semantic_payload(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> dict:
    from .semantic_memory import empty_semantic_delta
    delta = empty_semantic_delta()
    if runtime is not None:
        current_state = runtime.state.semantic_memory.as_dict()
        delta["progress"]["remaining"] = list(
            current_state["progress"]["remaining"]
        )
        delta["open_questions"] = list(current_state["open_questions"])
        delta["next_actions"] = list(current_state["next_actions"])
    uses = _collect_tool_uses(messages)
    result_by_id = {
        str(block.get("tool_use_id", "")): block
        for _, _, block in collect_tool_results(messages)
    }
    for tool_use_id, tool_use in uses.items():
        if tool_use.get("name") != "read_file":
            continue
        result = result_by_id.get(tool_use_id)
        if result is None:
            continue
        path = _normalized_read_path(tool_use)
        content = str(result.get("content", ""))
        if not path or not content:
            continue
        card = {
            "path": path,
            "digest": "",
            "stale": True,
            "source_tool_use_ids": [tool_use_id],
            "supersede_fields": [],
            "purpose": "Previously inspected during the task",
            "key_symbols": [],
            "key_behaviors": [],
            "important_conditions": [],
            "relationships": [],
            "relevant_ranges": [],
            "short_snippets": [content[:500]],
            "conclusions": [],
            "uncertainties": [
                "Fallback retained raw context; semantic interpretation is incomplete"
            ],
        }
        for line in content.splitlines():
            label, separator, value = line.partition(":")
            if not separator or not value.strip():
                continue
            lowered = label.strip().lower()
            target = {
                "role": "purpose",
                "purpose": "purpose",
                "condition": "important_conditions",
                "relationship": "relationships",
                "behavior": "key_behaviors",
                "decision": "conclusions",
            }.get(lowered)
            if target == "purpose":
                card[target] = value.strip()[:600]
            elif target:
                card[target].append(value.strip()[:600])
        delta["files"].append(card)
    text_messages = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text_messages.append(content)
        elif isinstance(content, list):
            for block in content:
                if block_type(block) == "text":
                    text_messages.append(str(_block_field(block, "text", "")))
    last_text = next(
        (text.strip() for text in reversed(text_messages) if text.strip()),
        "",
    )
    delta["progress"]["current_focus"] = last_text[:600]
    return {
        "conversation_checkpoint": {
            "summary": (
                "Structured compact output was unavailable; deterministic "
                "fallback retained the outgoing history's file snippets."
            ),
            "current_focus": last_text[:400],
            "remaining": [],
        },
        "semantic_memory_delta": delta,
        "_transfer_complete": False,
        "_transfer_failure": (
            "Structured semantic extraction was unavailable; raw history must "
            "be retained unless a caller explicitly accepts information loss."
        ),
    }


def summarize_history(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> dict:
    prompt = _compact_prompt(messages, runtime)
    try:
        raw = _call_compact_model(
            prompt, runtime=runtime, purpose="compact_summary",
        )
    except Exception:
        return _fallback_semantic_payload(messages, runtime)
    payload = _validate_compact_payload(_parse_json_object(raw))
    if payload is not None:
        return payload
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    repair_allowed, _ = can_spend_optional_calls(model_client, 1)
    if repair_allowed:
        repair_prompt = (
            "Repair the invalid compact output below. Return JSON only, exactly "
            "matching this shape. Do not add new facts.\n\nShape:\n"
            + json.dumps(
                _compact_output_schema(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\nInvalid output:\n"
            + raw
        )
        try:
            repaired = _call_compact_model(
                repair_prompt,
                runtime=runtime,
                purpose="compact_summary_repair",
            )
        except Exception:
            repaired = ""
        payload = _validate_compact_payload(_parse_json_object(repaired))
        if payload is not None:
            return payload
    record_event(
        "compact_summary_fallback",
        reason="invalid_structured_output",
    )
    return _fallback_semantic_payload(messages, runtime)


def _deterministic_history_summary(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> dict:
    payload = _fallback_semantic_payload(messages, runtime)
    todos = runtime.state.todos if runtime is not None else CURRENT_TODOS
    remaining = []
    completed = []
    for todo in list(todos)[:12]:
        text = str(todo.get("content", "")).strip()[:300]
        if not text:
            continue
        if todo.get("status") == "completed":
            completed.append(text)
        else:
            remaining.append(text)
    delta = payload["semantic_memory_delta"]
    delta["progress"]["completed"].extend(completed)
    delta["progress"]["remaining"].extend(remaining)
    payload["conversation_checkpoint"]["remaining"] = remaining[:8]
    payload["conversation_checkpoint"]["summary"] = (
        "Model-generated compact output was skipped to preserve the "
        "finalization-call reserve. Deterministic semantic fallback was used."
    )
    return payload


def _record_compact_event(
    kind: str, transcript: Path, messages: list, *, summary_mode: str = "model",
):
    try:
        record_event("compact",
                     kind=kind,
                     summary_mode=summary_mode,
                     transcript=str(transcript),
                     message_count=len(messages),
                     estimated_size=estimate_size(messages))
    except Exception:
        pass


def _coerce_compact_payload(
    value,
    history: list,
    runtime: AgentRuntime | None,
) -> dict:
    validated = None
    if isinstance(value, dict):
        validated = _validate_compact_payload({
            key: value[key]
            for key in ("conversation_checkpoint", "semantic_memory_delta")
            if key in value
        })
    if validated is not None:
        if value.get("_transfer_complete") is False:
            validated["_transfer_complete"] = False
            validated["_transfer_failure"] = str(
                value.get("_transfer_failure", "semantic transfer incomplete")
            )
        return validated
    # Compatibility for injected/fake summary functions used by embedders and
    # older tests. It is a checkpoint only and must not masquerade as semantic
    # state.
    if isinstance(value, str) and value.strip():
        payload = _fallback_semantic_payload([], runtime)
        payload["conversation_checkpoint"]["summary"] = value[:1200]
        return payload
    return _fallback_semantic_payload(history, runtime)


def _merge_semantic_payload(
    payload: dict,
    history: list,
    runtime: AgentRuntime | None,
) -> None:
    if runtime is None:
        return
    runtime.state.semantic_memory.merge(
        payload["semantic_memory_delta"],
        observations={
            tool_use_id: runtime.state.read_observations[tool_use_id]
            for tool_use_id in _history_observations(history, runtime)
        },
        current_digests=_current_file_digests(runtime),
    )


def _checkpoint_message(payload: dict, *, reactive: bool = False) -> dict:
    checkpoint = payload["conversation_checkpoint"]
    rendered = json.dumps(
        checkpoint,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # Validation already bounds each field. This loop keeps the compact message
    # bounded without cutting JSON or duplicating canonical semantic memory.
    while len(rendered) > CHECKPOINT_LIMIT and checkpoint["remaining"]:
        checkpoint = dict(checkpoint)
        checkpoint["remaining"] = checkpoint["remaining"][:-1]
        rendered = json.dumps(
            checkpoint, ensure_ascii=False, separators=(",", ":"),
        )
    if len(rendered) > CHECKPOINT_LIMIT:
        checkpoint = {
            "summary": checkpoint.get("summary", "")[:800],
            "current_focus": checkpoint.get("current_focus", "")[:300],
            "remaining": [],
        }
        rendered = json.dumps(
            checkpoint, ensure_ascii=False, separators=(",", ":"),
        )
    label = "[Reactive checkpoint]" if reactive else "[Compacted checkpoint]"
    return {"role": "user", "content": f"{label}\n{rendered}"}


def _split_text(value: str, max_chars: int) -> list[str]:
    text = str(value)
    return [
        text[index:index + max_chars]
        for index in range(0, len(text), max_chars)
    ] or [""]


def _split_oversized_unit(
    unit: list[dict],
    *,
    max_chars: int,
) -> list[list[dict]] | None:
    """Split content while preserving complete Tool-use/result exchanges."""
    if estimate_size(unit) <= max_chars:
        return [unit]
    if (
        len(unit) == 2
        and message_has_tool_use(unit[0])
        and is_tool_result_message(unit[1])
    ):
        uses = {
            str(_block_field(block, "id", "")): block
            for block in unit[0].get("content", [])
            if block_type(block) == "tool_use"
        }
        pieces = []
        for result in unit[1].get("content", []):
            if not isinstance(result, dict) or result.get("type") != "tool_result":
                continue
            tool_use_id = str(result.get("tool_use_id", ""))
            use = uses.get(tool_use_id)
            if use is None:
                return None
            overhead = estimate_size([
                {"role": "assistant", "content": [use]},
                {"role": "user", "content": [{
                    **result, "content": "",
                }]},
            ])
            chunk_size = max_chars - overhead - 300
            if chunk_size < 1000:
                return None
            chunks = _split_text(str(result.get("content", "")), chunk_size)
            for index, chunk in enumerate(chunks):
                chunk_result = deepcopy(result)
                chunk_result["content"] = (
                    f"[semantic extraction chunk {index + 1}/{len(chunks)} "
                    f"for tool result {tool_use_id}]\n{chunk}"
                )
                pieces.append([
                    {"role": "assistant", "content": [deepcopy(use)]},
                    {"role": "user", "content": [chunk_result]},
                ])
        return pieces or None
    if len(unit) == 1 and isinstance(unit[0].get("content"), str):
        message = unit[0]
        overhead = estimate_size([{**message, "content": ""}])
        chunk_size = max_chars - overhead - 200
        if chunk_size < 1000:
            return None
        chunks = _split_text(message["content"], chunk_size)
        return [[{
            **message,
            "content": (
                f"[semantic extraction chunk {index + 1}/{len(chunks)}]\n"
                f"{chunk}"
            ),
        }] for index, chunk in enumerate(chunks)]
    return None


def _extraction_batches(
    history: list,
    *,
    max_chars: int = COMPACT_INPUT_LIMIT,
) -> tuple[
    list[tuple[list[dict], dict[int, int]]],
    list[list[dict]],
    dict[int, int],
]:
    """Return bounded batches, original units, and piece counts by unit."""
    original_units = [
        unit for unit in _history_units(history)
        if not _is_prior_checkpoint(unit)
    ]
    pieces: list[tuple[int, list[dict]]] = []
    piece_counts: dict[int, int] = {}
    for unit_index, unit in enumerate(original_units):
        split = _split_oversized_unit(unit, max_chars=max_chars)
        if split is None:
            piece_counts[unit_index] = 0
            continue
        piece_counts[unit_index] = len(split)
        pieces.extend((unit_index, item) for item in split)
    batches: list[tuple[list[dict], dict[int, int]]] = []
    current_messages: list[dict] = []
    current_units: dict[int, int] = {}
    current_size = 0
    for unit_index, piece in pieces:
        piece_size = estimate_size(piece)
        if current_messages and current_size + piece_size > max_chars:
            batches.append((current_messages, current_units))
            current_messages = []
            current_units = {}
            current_size = 0
        current_messages.extend(piece)
        current_units[unit_index] = current_units.get(unit_index, 0) + 1
        current_size += piece_size
    if current_messages:
        batches.append((current_messages, current_units))
    return batches, original_units, piece_counts


def _compact_outgoing_history(
    history: list,
    *,
    allow_model_summary: bool,
    runtime: AgentRuntime | None,
) -> tuple[dict, list]:
    batches, units, piece_counts = _extraction_batches(
        history,
        max_chars=COMPACT_HISTORY_CHUNK_LIMIT,
    )
    succeeded = {index: 0 for index in range(len(units))}
    payload = _deterministic_history_summary([], runtime)
    processed_batches = 0
    if allow_model_summary:
        for batch, unit_piece_counts in batches:
            if processed_batches >= MAX_COMPACT_PASSES:
                break
            model_client = (
                runtime.services.model_client if runtime is not None else client
            )
            allowed, _ = can_spend_optional_calls(model_client, 1)
            if not allowed:
                break
            raw_payload = (
                summarize_history(batch, runtime)
                if runtime is not None else summarize_history(batch)
            )
            current_payload = _coerce_compact_payload(
                raw_payload, batch, runtime,
            )
            payload = current_payload
            processed_batches += 1
            expected_tool_ids = {
                str(block.get("tool_use_id", ""))
                for _, _, block in collect_tool_results(batch)
                if str(block.get("tool_use_id", ""))
            }
            acknowledged_tool_ids = set(
                current_payload["semantic_memory_delta"].get(
                    "processed_tool_use_ids", ()
                )
            )
            acknowledged = expected_tool_ids <= acknowledged_tool_ids
            if (
                current_payload.get("_transfer_complete") is True
                and acknowledged
            ):
                _merge_semantic_payload(current_payload, batch, runtime)
                for unit_index, count in unit_piece_counts.items():
                    succeeded[unit_index] += count
            elif expected_tool_ids and not acknowledged:
                current_payload["_transfer_complete"] = False
                current_payload["_transfer_failure"] = (
                    "compact output did not acknowledge every tool result in "
                    "the extraction batch"
                )
    retained_units = [
        unit for index, unit in enumerate(units)
        if piece_counts.get(index, 0) == 0
        or succeeded.get(index, 0) < piece_counts[index]
    ]
    if retained_units:
        payload = dict(payload)
        payload["_transfer_complete"] = False
        payload["_transfer_failure"] = (
            f"{len(retained_units)} complete history unit(s) could not be "
            "semantically transferred within the compact call budget"
        )
    return (
        payload,
        [message for unit in retained_units for message in unit],
    )


def _history_and_recent_tail(messages: list, keep_tail: int):
    tail_start = max(0, len(messages) - keep_tail)
    if (tail_start > 0 and tail_start < len(messages)
            and is_tool_result_message(messages[tail_start])
            and message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    history, tail = messages[:tail_start], messages[tail_start:]
    # A short conversation can still exceed the limit because of one huge
    # prompt or response. Summarize it whole instead of preserving the cause.
    if not history or estimate_size(tail) > int(CONTEXT_LIMIT * 0.6):
        return messages, []
    return history, tail


def _compact_with_postcondition(
    messages: list,
    *,
    allow_model_summary: bool,
    runtime: AgentRuntime | None,
    reactive: bool,
    target_context_budget: int | None,
    request_size_fn,
    system: str,
    tools: list | None,
) -> list:
    target = (
        max(1000, int(target_context_budget))
        if target_context_budget is not None
        else max(1000, CONTEXT_LIMIT - COMPACT_OUTPUT_RESERVE_CHARS)
    )
    sizer = request_size_fn or (
        lambda candidate: estimate_context_size(
            candidate, system=system, tools=tools,
        )
    )
    current = list(messages)
    previous_size = sizer(current)
    last_failure = ""
    for iteration in range(3):
        keep_tail = COMPACT_KEEP_TAIL_MESSAGES if iteration == 0 else 0
        history, tail = _history_and_recent_tail(current, keep_tail)
        payload, retained = _compact_outgoing_history(
            history,
            allow_model_summary=allow_model_summary,
            runtime=runtime,
        )
        candidate = [
            *retained,
            _checkpoint_message(payload, reactive=reactive),
            *tail,
        ]
        request_size = sizer(candidate)
        record_event(
            "compact_postcondition",
            kind="reactive" if reactive else "automatic",
            iteration=iteration + 1,
            target_context_budget=target,
            assembled_request_size=request_size,
            retained_messages=len(retained),
            transfer_complete=payload.get("_transfer_complete") is True,
        )
        current = candidate
        if request_size <= target:
            return candidate
        if retained:
            last_failure = str(payload.get(
                "_transfer_failure",
                "semantic extraction did not cover retained history",
            ))
            break
        if request_size >= previous_size and iteration > 0:
            last_failure = (
                "compaction made no progress; fixed system/tool/dynamic "
                "context or the bounded checkpoint exceeds the target"
            )
            break
        previous_size = request_size
    if not last_failure:
        last_failure = "maximum compact iterations reached"
    record_event(
        "compact_postcondition_failed",
        kind="reactive" if reactive else "automatic",
        target_context_budget=target,
        assembled_request_size=sizer(current),
        reason=last_failure,
    )
    raise ContextCompactionError(
        "Context compact could not satisfy target budget "
        f"{target}: {last_failure}"
    )


def compact_history(
    messages: list, *, allow_model_summary: bool | None = None,
    reason: str = "", runtime: AgentRuntime | None = None,
    target_context_budget: int | None = None,
    request_size_fn=None,
    system: str = "",
    tools: list | None = None,
) -> list:
    transcript = write_transcript(messages, runtime)
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    if allow_model_summary is None:
        allow_model_summary, budget = can_spend_optional_calls(
            model_client, 1,
        )
    else:
        budget = {}
    summary_mode = "model" if allow_model_summary else "deterministic"
    _record_compact_event(
        "automatic", transcript, messages, summary_mode=summary_mode)
    print(f"  \033[36m[compact] transcript saved: {transcript}\033[0m")
    if not allow_model_summary:
        record_event(
            "model_budget_guard", decision="deterministic_compact",
            reason=reason or "finalization_reserve",
            **{key: value for key, value in budget.items()
               if key != "available"},
        )
    return _compact_with_postcondition(
        messages,
        allow_model_summary=bool(allow_model_summary),
        runtime=runtime,
        reactive=False,
        target_context_budget=target_context_budget,
        request_size_fn=request_size_fn,
        system=system,
        tools=tools,
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
    transcript = write_transcript(messages, runtime)
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    allow_model_summary, budget = can_spend_optional_calls(model_client, 1)
    summary_mode = "model" if allow_model_summary else "deterministic"
    _record_compact_event(
        "reactive", transcript, messages, summary_mode=summary_mode)
    print(f"  \033[31m[reactive compact] transcript saved: {transcript}\033[0m")
    if not allow_model_summary:
        record_event(
            "model_budget_guard", decision="deterministic_reactive_compact",
            reason="finalization_reserve",
            **{key: value for key, value in budget.items()
               if key != "available"},
        )
    return _compact_with_postcondition(
        messages,
        allow_model_summary=bool(allow_model_summary),
        runtime=runtime,
        reactive=True,
        target_context_budget=target_context_budget,
        request_size_fn=request_size_fn,
        system=system,
        tools=tools,
    )



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
