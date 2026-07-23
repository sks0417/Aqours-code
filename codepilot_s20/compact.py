from .runtime_state import *
from .model_budget import can_spend_optional_calls
from .runtime import AgentRuntime

# ── Context Compaction ──

# Compaction is layered: first shrink oversized tool results, then trim old
# message ranges, and only call the model for a summary when the context is
# still too large or the model explicitly asks for compact.
def estimate_size(messages: list) -> int:
    return len(json.dumps(messages, default=str))

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


def _batch_counts_toward_recent(blocks: list, tool_uses: dict) -> bool:
    """Return whether a result batch carries working-set evidence.

    A pure todo update is control-plane state, not repository evidence. Unknown
    tool results remain protected so schema drift cannot silently discard them.
    Plain reminders and notifications are not tool-result batches and therefore
    never reach this classifier.
    """
    for block in blocks:
        tool_use = tool_uses.get(str(block.get("tool_use_id", "")))
        if tool_use is None or tool_use["name"] != "todo_write":
            return True
    return False


def _recent_read_working_set_indices(batches: list, tool_uses: dict,
                                     max_paths: int) -> set[int]:
    """Keep the newest bounded set of distinct file reads available.

    Tool-result batches can be very uneven: one turn may read a dozen related
    source files while the next reads one test. Protecting only a fixed number
    of messages lets that one-file turn evict the repository working set. A
    path bound preserves breadth without adding a summary model call or an
    unbounded live prompt.
    """
    protected = set()
    seen_paths = set()
    for message_index, _, blocks in reversed(batches):
        batch_paths = []
        for block in reversed(blocks):
            if len(str(block.get("content", ""))) <= 120:
                continue
            tool_use = tool_uses.get(str(block.get("tool_use_id", "")))
            if not tool_use or tool_use["name"] != "read_file":
                continue
            path = _normalized_read_path(tool_use)
            if path and path not in seen_paths and path not in batch_paths:
                batch_paths.append(path)
        if not batch_paths or len(seen_paths) >= max_paths:
            continue
        protected.add(message_index)
        # A message is the atomic compaction unit, so retain every path in a
        # selected wide batch even if it crosses the soft path bound.
        seen_paths.update(batch_paths)
    return protected


def _normalized_read_path(tool_use: dict) -> str:
    path = str(tool_use.get("input", {}).get("path", "")).replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    return path.rstrip("/")


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
    blocks = batches[-1][2]
    originals = [(block, str(block.get("content", ""))) for block in blocks]
    for block, text in originals:
        if len(text) > PERSIST_THRESHOLD:
            block["content"] = persist_large_output(
                block.get("tool_use_id", "unknown"), text,
                runtime=runtime)

    def total_size():
        return sum(len(str(block.get("content", ""))) for block in blocks)

    total = total_size()
    if total <= max_bytes:
        return messages

    per_result_preview = max(0, max_bytes // max(1, len(blocks)) - 512)
    for block, original in sorted(originals,
                                  key=lambda pair: len(pair[1]),
                                  reverse=True):
        if total <= max_bytes:
            break
        candidate = persist_large_output(
            block.get("tool_use_id", "unknown"), original,
            force=True, preview_chars=per_result_preview,
            runtime=runtime)
        if len(candidate) < len(str(block.get("content", ""))):
            block["content"] = candidate
            total = total_size()

    # Very wide tool batches may still exceed the budget after every result
    # gets an equal preview. Drop previews oldest-first while retaining the
    # persisted path and result identity for every block.
    if total > max_bytes:
        for block, original in originals:
            if total <= max_bytes:
                break
            candidate = persist_large_output(
                block.get("tool_use_id", "unknown"), original,
                force=True, preview_chars=0, runtime=runtime)
            if len(candidate) < len(str(block.get("content", ""))):
                block["content"] = candidate
                total = total_size()
    return messages


def snip_compact(messages: list, max_messages: int | None = None,
                 trigger_size: int | None = None) -> list:
    max_messages = SNIP_MAX_MESSAGES if max_messages is None else int(max_messages)
    trigger_size = (MICRO_COMPACT_TRIGGER if trigger_size is None
                    else int(trigger_size))
    if len(messages) <= max_messages or estimate_size(messages) <= trigger_size:
        return messages
    head_end, tail_start = 3, len(messages) - (max_messages - 3)
    if head_end > 0 and message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and is_tool_result_message(messages[head_end]):
            head_end += 1
    if (tail_start > 0 and tail_start < len(messages)
            and is_tool_result_message(messages[tail_start])
            and message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    if head_end >= tail_start:
        return messages
    snipped = tail_start - head_end
    return (messages[:head_end]
            + [{"role": "user", "content": f"[snipped {snipped} messages]"}]
            + messages[tail_start:])


def _compacted_result_text(content) -> str:
    text = str(content)
    persisted_path = next(
        (line for line in text.splitlines() if line.startswith("Full output: ")),
        "",
    )
    marker = "[Earlier tool result compacted after it was consumed.]"
    return f"{marker}\n{persisted_path}" if persisted_path else marker


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
    if estimate_size(messages) <= target_size:
        return messages

    working_batches = [batch for batch in batches
                       if _batch_counts_toward_recent(batch[2], tool_uses)]
    protected_indices = {
        batch[0] for batch in working_batches[-KEEP_RECENT_TOOL_RESULT_MESSAGES:]
    }
    knowledge_paths = (
        sum(
            1 for record in runtime.state.knowledge.files.values()
            if record.evidence_valid
        )
        if runtime is not None else 0
    )
    protected_indices.update(_recent_read_working_set_indices(
        batches, tool_uses, max(KEEP_RECENT_READ_PATHS, knowledge_paths)))
    for message_index, _, blocks in batches:
        if message_index in protected_indices:
            continue
        for block in blocks:
            if len(str(block.get("content", ""))) > 120:
                block["content"] = _compacted_result_text(
                    block.get("content", ""))
        if estimate_size(messages) <= target_size:
            break
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


def summarize_history(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> str:
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue. "
              "Preserve current goal, key findings, changed files, remaining work, "
              "and user constraints. Preserve the inspected file/symbol map and "
              "exact contract facts involving any/all/different/unchanged, "
              "normalized or fingerprint fields, and exception/state branches. "
              "Distinguish verified code facts from assumptions so compacted "
              "history is not treated as proof.\n\n" + conversation)
    summary_messages = [{"role": "user", "content": prompt}]
    model = runtime.config.model if runtime is not None else MODEL
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    record_llm_request(
        model=model, max_tokens=2000, message_count=1, tool_count=0,
        purpose="compact_summary", agent_role="",
    )
    try:
        response = model_client.messages.create(
            model=model,
            messages=summary_messages,
            max_tokens=2000)
    except Exception as exc:
        record_event(
            "compact_summary_error", error_type=type(exc).__name__,
            error=str(exc)[:1000],
        )
        raise
    record_llm_response(response, purpose="compact_summary", agent_role="")
    return extract_text(response.content) or "(empty summary)"


def _deterministic_history_summary(
    runtime: AgentRuntime | None = None,
) -> str:
    root_task = str(
        runtime.state.root_task if runtime is not None else CURRENT_ROOT_TASK or ""
    ).strip()
    root_section = root_task[:5000] or "(root task unavailable)"
    todo_lines = []
    todos = runtime.state.todos if runtime is not None else CURRENT_TODOS
    for todo in list(todos)[:12]:
        content = str(todo.get("content", ""))[:240]
        evidence = str(todo.get("evidence", ""))[:240]
        todo_id = str(todo.get("id", "todo"))[:100]
        line = (
            f"- [{todo_id} {todo.get('status', 'pending')}] "
            f"{todo.get('kind', 'plan')}: {content}"
        )
        if evidence:
            line += f" | evidence: {evidence}"
        todo_lines.append(line)
    todos = "\n".join(todo_lines) or "- (no live todos)"
    return (
        "Model-generated history summary was skipped to preserve the finalization "
        "call reserve. Continue from the authoritative root task, live checklist, "
        "and recent messages. Do not restart repository exploration.\n\n"
        f"Root task:\n{root_section}\n\nLive checklist:\n{todos}"
    )


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


def _working_memory_notice(runtime: AgentRuntime | None) -> str:
    if runtime is None:
        return ""
    knowledge = runtime.state.knowledge
    valid_files = sum(
        1 for item in knowledge.files.values() if item.evidence_valid
    )
    stale_files = len(knowledge.files) - valid_files
    return (
        "\n\n[RunKnowledge retained outside raw message history: "
        f"{valid_files} valid files, {stale_files} stale files, "
        f"{len(knowledge.modified_files)} modified files, "
        f"{len(knowledge.recent_tests)} recent tests, "
        f"{len(knowledge.reviewer_findings)} reviewer findings. "
        "The authoritative structured state is injected on every turn.]"
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


def compact_history(
    messages: list, *, allow_model_summary: bool | None = None,
    reason: str = "", runtime: AgentRuntime | None = None,
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
    history, tail = _history_and_recent_tail(
        messages, COMPACT_KEEP_TAIL_MESSAGES)
    if allow_model_summary:
        summary = (
            summarize_history(history, runtime)
            if runtime is not None else summarize_history(history)
        )
    else:
        summary = _deterministic_history_summary(runtime)
        record_event(
            "model_budget_guard", decision="deterministic_compact",
            reason=reason or "finalization_reserve",
            **{key: value for key, value in budget.items()
               if key != "available"},
        )
    summary += _working_memory_notice(runtime)
    return [
        {"role": "user", "content": f"[Compacted]\n\n{summary}"},
        *tail,
    ]


def reactive_compact(
    messages: list,
    runtime: AgentRuntime | None = None,
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
    history, tail = _history_and_recent_tail(
        messages, COMPACT_KEEP_TAIL_MESSAGES)
    if allow_model_summary:
        try:
            summary = (
                summarize_history(history, runtime)
                if runtime is not None else summarize_history(history)
            )
        except Exception:
            summary = "Earlier conversation was trimmed after a prompt-too-long error."
    else:
        summary = _deterministic_history_summary(runtime)
        record_event(
            "model_budget_guard", decision="deterministic_reactive_compact",
            reason="finalization_reserve",
            **{key: value for key, value in budget.items()
               if key != "available"},
        )
    summary += _working_memory_notice(runtime)
    return [
        {"role": "user", "content": f"[Reactive compact]\n\n{summary}"},
        *tail,
    ]



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
