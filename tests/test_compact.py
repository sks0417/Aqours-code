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


def long_history(count: int = 10, width: int = 350) -> list[dict]:
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
        target_context_budget=kwargs.pop("target_context_budget", 12_000),
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


def test_automatic_trigger_uses_complete_request_size(monkeypatch):
    calls = install_summary(monkeypatch)
    messages = long_history()

    result = compact.compact_history(
        messages,
        allow_model_summary=True,
        target_context_budget=49_000,
        request_size_fn=lambda candidate: compact.estimate_size(candidate) + 43_000,
    )

    assert calls
    assert checkpoint_count(result) == 1


def test_successful_compact_has_checkpoint_and_recent_raw_tail(monkeypatch):
    calls = install_summary(
        monkeypatch,
        "## Handoff\n- changed `src/a.py`\n- tests pass",
    )
    messages = long_history()
    expected_tail = messages[-(compact.RECENT_TOOL_RESULT_COUNT * 2):]

    result = force_compact(messages)

    assert len(calls) == 1
    assert checkpoint_count(result) == 1
    assert "## Handoff" in render(result)
    assert result[-len(expected_tail):] == expected_tail
    assert_tool_pairs(result)


def test_compaction_prompt_requires_concrete_self_contained_markdown(
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
    assert "Do not merely state that a file was inspected." in captured["prompt"]
    assert "self-contained Markdown" in captured["prompt"]
    assert "Return concise Markdown only" in captured["prompt"]
    assert "archive IDs" in captured["prompt"]
    assert "recovery tool" not in captured["prompt"]


def test_prior_checkpoint_is_folded_into_replacement_without_stacking(
    monkeypatch,
):
    calls = install_summary(monkeypatch, "first cumulative checkpoint")
    first = force_compact(long_history(10))
    assert checkpoint_count(first) == 1

    for index in range(10, 16):
        first.extend(exchange(index, "new fact " + "y" * 300))
    calls.clear()
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda messages, runtime=None: (
            calls.append(json.loads(json.dumps(messages)))
            or "second cumulative checkpoint"
        ),
    )

    second = force_compact(first)

    assert compact.CONTEXT_CHECKPOINT_MARKER in render(calls[0])
    assert checkpoint_count(second) == 1
    assert "second cumulative checkpoint" in render(second)


def test_latest_user_instruction_is_retained_verbatim(monkeypatch):
    install_summary(monkeypatch)
    messages = [{"role": "user", "content": "LATEST USER REQUIREMENT"}]
    for index in range(12):
        messages.extend(exchange(index, "z" * 300))

    result = force_compact(messages)

    assert "LATEST USER REQUIREMENT" in render(result)
    assert checkpoint_count(result) == 1


def test_recent_four_tool_exchanges_remain_verbatim(monkeypatch):
    install_summary(monkeypatch)
    messages = long_history(12, width=250)
    expected = messages[-8:]

    result = force_compact(messages)

    assert result[-8:] == expected
    assert_tool_pairs(result)


def test_cut_point_never_splits_tool_exchange(monkeypatch):
    install_summary(monkeypatch)

    result = force_compact(long_history(14, width=300))

    assert_tool_pairs(result)


def test_parallel_tool_calls_remain_one_atomic_exchange(monkeypatch):
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

    result = force_compact(messages)

    assert result[-2:] == [multi_use, multi_result]
    assert_tool_pairs(result)


def test_oversized_recent_tool_result_becomes_short_placeholder(
    tmp_path,
    monkeypatch,
):
    install_summary(monkeypatch)
    runtime = make_runtime(tmp_path)
    huge = "H" * (
        compact.MAX_TOOL_RESULT_TOKENS
        * compact.ESTIMATED_CHARS_PER_TOKEN
        + 3
    )
    messages = long_history(8, width=100)
    messages.extend(exchange(99, huge))
    original = json.loads(json.dumps(messages))

    result = force_compact(messages, runtime=runtime)

    result_block = result[-1]["content"][0]
    assert result_block["tool_use_id"] == "tool-99"
    assert result_block["content"].startswith("[Large tool result omitted]")
    assert "estimated tokens" in result_block["content"]
    assert len(result_block["content"]) < 200
    assert messages == original
    assert messages[-1]["content"][0]["content"] == huge
    assert_tool_pairs(result)
    transcript = next(runtime.paths.transcript_dir.glob("transcript_*.jsonl"))
    transcript_text = transcript.read_text(encoding="utf-8")
    assert huge not in transcript_text
    assert "[Large tool result omitted]" in transcript_text


def test_oversized_prefix_result_is_masked_only_in_summary_copy(monkeypatch):
    calls = install_summary(monkeypatch)
    huge = "P" * (
        compact.MAX_TOOL_RESULT_TOKENS
        * compact.ESTIMATED_CHARS_PER_TOKEN
        + 3
    )
    messages = [
        {"role": "user", "content": "Keep this task."},
        *exchange(0, huge),
    ]
    for index in range(1, 8):
        messages.extend(exchange(index, "normal"))
    original = json.loads(json.dumps(messages))

    force_compact(messages)

    assert "[Large tool result omitted]" in render(calls[0])
    assert huge not in render(calls[0])
    assert messages == original


def test_normal_recent_tool_result_is_not_modified(monkeypatch):
    install_summary(monkeypatch)
    messages = long_history(8, width=100)
    recent = exchange(50, "precise recent output")
    messages.extend(recent)

    result = force_compact(messages)

    assert result[-2:] == recent


@pytest.mark.parametrize(
    "summary_behavior",
    [
        lambda _messages, runtime=None: (_ for _ in ()).throw(
            RuntimeError("offline")
        ),
        lambda _messages, runtime=None: "   ",
        lambda _messages, runtime=None: "s" * 30_000,
    ],
)
def test_compaction_failure_keeps_original_history(
    monkeypatch,
    summary_behavior,
):
    calls = 0

    def counted(messages, runtime=None):
        nonlocal calls
        calls += 1
        return summary_behavior(messages, runtime)

    monkeypatch.setattr(compact, "summarize_history", counted)
    messages = long_history(15, width=400)
    original = json.loads(json.dumps(messages))

    result = force_compact(messages)

    assert calls == 1
    assert result is messages
    assert messages == original


def test_unsafe_summary_input_fails_before_model_call(monkeypatch):
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: pytest.fail("unsafe request must not be sent"),
    )
    messages = [
        {"role": "user", "content": "PINNED-" + "p" * 60_000},
        *long_history(6)[1:],
    ]
    original = json.loads(json.dumps(messages))

    result = force_compact(messages)

    assert result is messages
    assert messages == original


def test_each_compact_uses_at_most_one_summary_model_call(monkeypatch):
    budget_requests = []
    calls = install_summary(monkeypatch, "s" * 30_000)
    monkeypatch.setattr(
        compact,
        "can_spend_optional_calls",
        lambda _client, count: (
            budget_requests.append(count) or (True, {"available": True})
        ),
    )

    force_compact(long_history(15, width=400))

    assert len(calls) == 1
    assert budget_requests == [1]


def test_compacted_request_satisfies_complete_assembled_target(monkeypatch):
    install_summary(monkeypatch, "bounded checkpoint")
    system_and_tools_overhead = 700

    def assembled_size(candidate):
        return compact.estimate_size(candidate) + system_and_tools_overhead

    result = force_compact(
        long_history(15, width=300),
        target_context_budget=12_000,
        request_size_fn=assembled_size,
    )

    assert assembled_size(result) <= 12_000


def test_reactive_compact_forces_compaction_below_automatic_trigger(
    monkeypatch,
):
    calls = install_summary(monkeypatch, "reactive checkpoint")
    messages = long_history(8, width=200)

    result = compact.reactive_compact(
        messages,
        target_context_budget=10_000,
        request_size_fn=compact.estimate_size,
    )

    assert len(calls) == 1
    assert "reactive checkpoint" in render(result)


def test_update_context_does_not_inject_working_or_semantic_memory(tmp_path):
    runtime = make_runtime(tmp_path)

    live = context.update_context({}, [], runtime)

    assert "working_memory" not in live
    assert "working_memory_prompt" not in live
    assert "semantic_memory" not in live
    assert "semantic_memory_prompt" not in live


def test_trace_metrics_and_compact_generation_are_retained(
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

    event = next(item for item in events if item["type"] == "compact")
    assert event["reason"] == "manual"
    assert event["before_messages"] == len(messages)
    assert event["after_messages"] > 0
    assert event["summarized_prefix_messages"] > 0
    assert event["recent_tail_tokens"] > 0
    assert event["summary_length"] > 0
    assert event["summary_model_calls"] == 1
    assert event["success"] is True
    assert runtime.state.metadata["compact_generation"] == 1
    transcript = Path(event["transcript"])
    assert transcript.exists()
    assert len(transcript.read_text(encoding="utf-8").splitlines()) == len(messages)


def test_compaction_does_not_create_context_archive_directory(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    install_summary(monkeypatch)

    force_compact(long_history(), runtime=runtime)

    assert not (runtime.paths.state_root / ".codepilot" / "context-archives").exists()


def test_three_compactions_keep_exactly_one_checkpoint(monkeypatch):
    summaries = iter(("checkpoint one", "checkpoint two", "checkpoint three"))
    seen = []

    def summarize(messages, runtime=None):
        seen.append(json.loads(json.dumps(messages)))
        return next(summaries)

    monkeypatch.setattr(compact, "summarize_history", summarize)
    messages = long_history(10)
    for generation in range(3):
        messages = force_compact(messages)
        assert checkpoint_count(messages) == 1
        if generation < 2:
            for index in range(10 + generation * 6, 16 + generation * 6):
                messages.extend(exchange(index, "new fact " + "n" * 250))

    assert compact.CONTEXT_CHECKPOINT_MARKER in render(seen[1])
    assert compact.CONTEXT_CHECKPOINT_MARKER in render(seen[2])
    assert "checkpoint three" in render(messages)
    assert_tool_pairs(messages)
