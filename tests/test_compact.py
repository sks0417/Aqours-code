from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot_s20 import compact, context
from codepilot_s20.command_executor import LocalCommandExecutor
from codepilot_s20.runtime import AgentRuntime


def make_runtime(tmp_path: Path, responses=()) -> AgentRuntime:
    class Messages:
        def __init__(self, values):
            self.values = list(values)
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            value = self.values.pop(0)
            if isinstance(value, BaseException):
                raise value
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=value)],
                usage=None,
            )

    return AgentRuntime.create(
        workdir=tmp_path,
        state_root=tmp_path / "state",
        model_client=SimpleNamespace(messages=Messages(responses)),
        command_executor=LocalCommandExecutor(),
        model_provider="test",
        model="test",
        root_task="compact test",
    )


def exchange(index: int, result: str | None = None) -> list[dict]:
    tool_id = f"tool-{index}"
    return [
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_id,
                "name": "read_file",
                "input": {"path": f"src/{index}.py"},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result if result is not None else f"result-{index}",
            }],
        },
    ]


def long_history(count: int = 8, width: int = 350) -> list[dict]:
    messages = [{"role": "user", "content": "Keep the latest request exact."}]
    for index in range(count):
        messages.extend(exchange(index, f"fact-{index}-" + ("x" * width)))
    return messages


def render(messages: list) -> str:
    return json.dumps(messages, default=str, ensure_ascii=False)


def checkpoint_count(messages: list) -> int:
    return render(messages).count(compact.CONTEXT_CHECKPOINT_MARKER)


def assert_tool_pairs(messages: list) -> None:
    for index, message in enumerate(messages):
        if not compact.is_tool_result_message(message):
            continue
        assert index > 0
        previous = messages[index - 1]
        assert compact.message_has_tool_use(previous)
        assert compact._tool_result_ids(message) <= compact._tool_use_ids(previous)


def install_summary(monkeypatch, text="## Progress\nOlder work retained."):
    calls = []

    def summarize(messages, runtime=None):
        calls.append(json.loads(json.dumps(messages)))
        return text

    monkeypatch.setattr(compact, "summarize_history", summarize)
    return calls


def force_compact(messages, **kwargs):
    return compact.compact_history(
        messages,
        reason="manual",
        target_context_budget=kwargs.pop("target_context_budget", 5_000),
        request_size_fn=kwargs.pop("request_size_fn", compact.estimate_size),
        **kwargs,
    )


def test_small_history_below_trigger_is_not_compacted(monkeypatch):
    messages = [{"role": "user", "content": "small request"}]
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: pytest.fail("summary must not run"),
    )

    result = compact.compact_history(messages, allow_model_summary=True)

    assert result is messages
    assert checkpoint_count(result) == 0


def test_successful_compact_has_one_markdown_checkpoint(monkeypatch):
    calls = install_summary(
        monkeypatch,
        "## Handoff\n- changed `src/a.py`\n- tests pass",
    )

    result = force_compact(long_history())

    assert len(calls) == 1
    assert checkpoint_count(result) == 1
    assert "## Handoff" in render(result)
    assert "semantic_memory_delta" not in render(result)


def test_compaction_prompt_requests_plain_markdown_and_includes_tool_facts(
    monkeypatch,
):
    captured = {}

    def invoke(prompt, **_kwargs):
        captured["prompt"] = prompt
        return "## Checkpoint\nUseful fact."

    monkeypatch.setattr(compact, "_call_compact_model", invoke)
    summary = compact.summarize_history(
        exchange(1, "reservation requires an idempotency key"),
    )

    assert summary.startswith("## Checkpoint")
    assert "reservation requires an idempotency key" in captured["prompt"]
    assert "Return concise Markdown only" in captured["prompt"]
    assert "JSON shape" not in captured["prompt"]
    assert "processed_tool_use_ids" not in captured["prompt"]


def test_prior_checkpoint_is_folded_into_next_without_stacking(monkeypatch):
    calls = install_summary(monkeypatch, "first cumulative checkpoint")
    first = force_compact(long_history(10))
    assert checkpoint_count(first) == 1

    first.extend(exchange(99, "new fact " + "y" * 800))
    calls.clear()
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda messages, runtime=None: (
            calls.append(json.loads(json.dumps(messages)))
            or "second cumulative checkpoint"
        ),
    )
    second = force_compact(first, target_context_budget=2_200)

    assert compact.CONTEXT_CHECKPOINT_MARKER in render(calls[0])
    assert checkpoint_count(second) == 1
    assert "second cumulative checkpoint" in render(second)


def test_recent_tail_is_preserved_verbatim(monkeypatch):
    install_summary(monkeypatch)
    messages = long_history(9, width=400)
    recent = exchange(50, "precise recent output")
    messages.extend(recent)

    result = force_compact(messages, target_context_budget=5_000)

    assert result[-2:] == recent
    assert_tool_pairs(result)


def test_latest_user_request_is_preserved_even_outside_suffix(monkeypatch):
    install_summary(monkeypatch)
    messages = [{"role": "user", "content": "LATEST USER REQUIREMENT"}]
    for index in range(12):
        messages.extend(exchange(index, "z" * 500))

    result = force_compact(messages, target_context_budget=4_000)

    assert "LATEST USER REQUIREMENT" in render(result)
    assert checkpoint_count(result) == 1


def test_cut_point_never_splits_tool_exchange(monkeypatch):
    install_summary(monkeypatch)

    result = force_compact(long_history(14, width=300), target_context_budget=3_500)

    assert_tool_pairs(result)


def test_multiple_tool_calls_near_cut_remain_paired(monkeypatch):
    install_summary(monkeypatch)
    multi_use = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "a.py"}},
            {"type": "tool_use", "id": "b", "name": "read_file",
             "input": {"path": "b.py"}},
        ],
    }
    multi_result = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "A"},
            {"type": "tool_result", "tool_use_id": "b", "content": "B"},
        ],
    }
    messages = long_history(8, width=300)
    messages.extend((multi_use, multi_result))

    result = force_compact(messages, target_context_budget=4_000)

    assert_tool_pairs(result)
    if multi_result in result:
        index = result.index(multi_result)
        assert result[index - 1] == multi_use


def test_summary_failure_keeps_original_history(monkeypatch):
    messages = long_history()
    original = json.loads(json.dumps(messages))
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    result = force_compact(messages)

    assert result is messages
    assert messages == original


def test_empty_summary_keeps_original_history(monkeypatch):
    messages = long_history()
    install_summary(monkeypatch, "   ")

    result = force_compact(messages)

    assert result is messages
    assert checkpoint_count(result) == 0


def test_summary_overflow_drops_only_one_complete_old_unit(monkeypatch):
    attempts = []

    def summarize(messages, runtime=None):
        attempts.append(json.loads(json.dumps(messages)))
        if len(attempts) == 1:
            raise RuntimeError("context_length_exceeded")
        return "overflow recovery succeeded"

    monkeypatch.setattr(compact, "summarize_history", summarize)

    result = force_compact(long_history(12), target_context_budget=4_000)

    assert len(attempts) == 2
    assert len(compact._history_units(attempts[0])) == (
        len(compact._history_units(attempts[1])) + 1
    )
    assert_tool_pairs(attempts[1])
    assert "overflow recovery succeeded" in render(result)


def test_oversized_tool_result_is_persisted_with_locatable_preview(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    install_summary(monkeypatch)
    monkeypatch.setattr(compact, "PERSIST_THRESHOLD", 100)
    monkeypatch.setattr(compact, "PERSIST_PREVIEW_CHARS", 40)
    huge = "HEAD-" + ("q" * 500) + "-TAIL"
    messages = long_history(5, width=150)
    messages.extend(exchange(80, huge))

    result = force_compact(
        messages,
        runtime=runtime,
        target_context_budget=4_000,
    )

    output_path = runtime.paths.tool_results_dir / "tool-80.txt"
    assert output_path.read_text(encoding="utf-8") == huge
    text = render(result)
    retained_result = next(
        str(block["content"])
        for _, _, block in compact.collect_tool_results(result)
        if block["tool_use_id"] == "tool-80"
    )
    assert str(output_path) in retained_result
    assert "Character count: 510" in text
    assert "Source tool result: tool-80" in text
    assert huge not in text


def test_normal_tool_results_are_not_silently_removed_on_ordinary_turn(
    monkeypatch,
):
    messages = exchange(1, "ordinary precise output")
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: pytest.fail("summary must not run"),
    )

    result = compact.compact_history(messages, allow_model_summary=True)

    assert result is messages
    assert "ordinary precise output" in render(result)


def test_compacted_request_satisfies_assembled_target(monkeypatch):
    install_summary(monkeypatch, "bounded checkpoint")
    system_overhead = 700

    def assembled_size(candidate):
        return compact.estimate_size(candidate) + system_overhead

    result = force_compact(
        long_history(15, width=300),
        target_context_budget=4_500,
        request_size_fn=assembled_size,
    )

    assert assembled_size(result) <= 4_500


def test_reactive_compact_forces_compaction_below_automatic_trigger(
    monkeypatch,
):
    calls = install_summary(monkeypatch, "reactive checkpoint")
    messages = long_history(4, width=100)

    result = compact.reactive_compact(
        messages,
        target_context_budget=2_000,
        request_size_fn=compact.estimate_size,
    )

    assert calls
    assert "reactive checkpoint" in render(result)


def test_update_context_does_not_inject_working_or_semantic_memory(tmp_path):
    runtime = make_runtime(tmp_path)

    live = context.update_context({}, [], runtime)

    assert "working_memory" not in live
    assert "working_memory_prompt" not in live
    assert "semantic_memory" not in live
    assert "semantic_memory_prompt" not in live


def test_transcript_and_compact_trace_record_required_metrics(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    install_summary(monkeypatch, "trace checkpoint")
    events = []
    monkeypatch.setattr(
        compact,
        "record_event",
        lambda event_type, **payload: events.append({
            "type": event_type,
            **payload,
        }),
    )
    messages = long_history()

    force_compact(messages, runtime=runtime)

    compact_event = next(event for event in events if event["type"] == "compact")
    assert compact_event["reason"] == "manual"
    assert compact_event["before_messages"] == len(messages)
    assert compact_event["after_messages"] > 0
    assert compact_event["summarized_prefix_messages"] > 0
    assert compact_event["recent_tail_tokens"] > 0
    assert compact_event["summary_length"] > 0
    assert compact_event["success"] is True
    transcript = Path(compact_event["transcript"])
    assert transcript.exists()
    assert len(transcript.read_text(encoding="utf-8").splitlines()) == len(messages)


def test_two_consecutive_compactions_keep_one_checkpoint(monkeypatch):
    summaries = iter(("checkpoint one", "checkpoint two"))
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda messages, runtime=None: next(summaries),
    )
    messages = long_history(10)
    messages[0]["content"] = [{
        "type": "text",
        "text": "Keep the latest list-form request exact.",
    }]
    first = force_compact(messages, target_context_budget=4_000)
    first.extend(long_history(5, width=250)[1:])

    second = force_compact(first, target_context_budget=3_500)

    assert checkpoint_count(second) == 1
    assert "checkpoint two" in render(second)
    assert_tool_pairs(second)
