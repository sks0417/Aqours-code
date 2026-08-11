from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aqours_code import agent_loop, compact, context
from aqours_code.command_executor import LocalCommandExecutor
from aqours_code.runtime import AgentRuntime


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
        target_context_budget=kwargs.pop("target_context_budget", 120_000),
        request_size_fn=kwargs.pop("request_size_fn", compact.estimate_size),
        **kwargs,
    )


def test_default_context_and_summary_budgets_are_independent():
    trigger_chars = (
        compact.COMPACT_TRIGGER_TOKENS
        * compact.ESTIMATED_CHARS_PER_TOKEN
    )

    assert compact.AGENT_CONTEXT_LIMIT_TOKENS == 128_000
    assert compact.CONTEXT_LIMIT_TOKENS == 128_000
    assert compact.CONTEXT_LIMIT == 384_000
    assert compact.COMPACT_TRIGGER_TOKENS == 100_000
    assert compact.COMPACT_TRIGGER_RATIO == 100_000 / 128_000
    assert compact.SUMMARY_INPUT_LIMIT_TOKENS == 256_000
    assert compact.SUMMARY_MAX_TOKENS == 6_000
    assert compact.estimate_context_tokens(compact.CONTEXT_LIMIT) == 128_000
    assert compact.estimate_context_tokens(trigger_chars) == 100_000
    assert 128_000 - 100_000 >= agent_loop.DEFAULT_MAX_TOKENS
    assert compact.RECENT_TAIL_MAX_TOKENS == 20_000


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
        target_context_budget=350_000,
        request_size_fn=lambda candidate: (
            compact.estimate_size(candidate) + 310_000
        ),
    )

    assert calls
    assert checkpoint_count(result) == 1


@pytest.mark.parametrize(
    ("estimated_tokens", "should_compact"),
    [(99_999, False), (100_000, True), (100_001, True)],
)
def test_automatic_compact_uses_token_threshold(
    monkeypatch,
    estimated_tokens,
    should_compact,
):
    calls = install_summary(monkeypatch)
    messages = long_history()

    compact.compact_history(
        messages,
        allow_model_summary=True,
        target_context_budget=350_000,
        request_size_fn=lambda _candidate: (
            estimated_tokens * compact.ESTIMATED_CHARS_PER_TOKEN
        ),
    )

    assert bool(calls) is should_compact


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
    assert result[0] == {
        "role": "user",
        "content": (
            compact.CONTEXT_CHECKPOINT_MARKER
            + "\n## Handoff\n- changed `src/a.py`\n- tests pass"
        ),
    }
    assert result[:2] == [
        result[0],
        messages[0],
    ]
    assert result[1] is not messages[0]
    assert (
        compact.MAX_TOOL_RESULT_TOKENS
        < compact.RECENT_TAIL_MAX_TOKENS
    )
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


def test_summary_model_receives_configured_6000_output_tokens(tmp_path):
    runtime = make_runtime(tmp_path, responses=["## Checkpoint\nDone."])

    summary = compact.summarize_history(exchange(1, "useful result"), runtime)

    assert summary.startswith("## Checkpoint")
    assert runtime.services.model_client.messages.calls[0]["max_tokens"] == 6_000


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
    latest = {"role": "user", "content": "LATEST USER REQUIREMENT"}
    messages = [latest]
    for index in range(12):
        messages.extend(exchange(index, "z" * 300))

    result = force_compact(messages)

    assert result[1] == latest
    assert sum(message == latest for message in result) == 1
    assert checkpoint_count(result) == 1


def test_latest_user_message_already_in_recent_tail_is_not_duplicated(
    monkeypatch,
):
    install_summary(monkeypatch)
    messages = long_history(7, width=100)
    latest = {
        "role": "user",
        "content": "Use the new API contract exactly.",
    }
    messages.append(latest)
    messages.extend(exchange(20, "after-latest-20"))
    messages.extend(exchange(21, "after-latest-21"))

    result = force_compact(messages)

    assert result[1] == latest
    assert sum(message == latest for message in result) == 1
    assert result[-4:] == messages[-4:]


def test_latest_user_message_outside_raw_tail_budget_stays_original(
    monkeypatch,
):
    install_summary(monkeypatch)
    latest = {
        "role": "user",
        "content": "Keep this old-position instruction byte-for-byte.",
    }
    messages = [latest]
    result_chars = 7_000 * compact.ESTIMATED_CHARS_PER_TOKEN
    for index in range(6):
        messages.extend(exchange(index, str(index) * result_chars))

    result = force_compact(messages)

    assert result[1] == latest
    assert sum(message == latest for message in result) == 1
    assert compact.estimate_context_tokens(
        compact.estimate_size(result[2:])
    ) <= compact.RECENT_TAIL_MAX_TOKENS
    assert_tool_pairs(result)


@pytest.mark.parametrize(
    "literal",
    [
        "[Latest user instruction — verbatim]",
        "[/Latest user instruction]",
    ],
)
def test_old_marker_literals_are_ordinary_user_text(
    monkeypatch,
    literal,
):
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: "summary without literal recovery",
    )
    messages = [{"role": "user", "content": literal}]
    for generation in range(2):
        for index in range(generation * 6, generation * 6 + 6):
            messages.extend(exchange(index, "work-" + "x" * 100))
        messages = force_compact(messages)

    assert messages[1] == {"role": "user", "content": literal}
    assert sum(
        message == {"role": "user", "content": literal}
        for message in messages
    ) == 1


def test_latest_user_block_list_is_preserved_without_stringification(
    monkeypatch,
):
    install_summary(monkeypatch)
    latest = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Inspect this image exactly."},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "aW1hZ2U=",
                },
            },
            {"type": "text", "text": "Keep block order."},
        ],
    }
    messages = [latest, *long_history(8, width=100)[1:]]

    result = force_compact(messages)

    assert result[1] == latest
    assert isinstance(result[1]["content"], list)
    assert [block["type"] for block in result[1]["content"]] == [
        "text",
        "image",
        "text",
    ]


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


def test_prepare_context_limits_oversized_result_below_compact_trigger(
    monkeypatch,
):
    huge = "H" * (
        compact.MAX_TOOL_RESULT_TOKENS
        * compact.ESTIMATED_CHARS_PER_TOKEN
        + 3
    )
    messages = exchange(1, huge)
    summary_calls = []
    events = []
    monkeypatch.setattr(agent_loop, "assemble_tool_pool", lambda *args: ([], {}))
    monkeypatch.setattr(
        agent_loop, "assemble_system_prompt", lambda *args: "system"
    )
    monkeypatch.setattr(agent_loop, "update_context", lambda *args: {})
    monkeypatch.setattr(
        agent_loop,
        "compact_history",
        lambda *args, **kwargs: summary_calls.append(True),
    )
    monkeypatch.setattr(
        agent_loop,
        "record_event",
        lambda event_type, **payload: events.append({
            "type": event_type,
            **payload,
        }),
    )

    agent_loop.prepare_context(messages)

    assert summary_calls == []
    assert messages[1]["content"][0]["tool_use_id"] == "tool-1"
    assert messages[1]["content"][0]["content"].startswith(
        "[Large tool result omitted]"
    )
    assert_tool_pairs(messages)
    changed = [
        event for event in events
        if event["type"] == "context_compact"
    ]
    assert len(changed) == 1
    assert changed[0]["stage"] == "tool_result_limit"
    assert changed[0]["changed"] is True


@pytest.mark.parametrize("changes_history", [False, True])
def test_prepare_context_trace_changed_matches_compact_result(
    monkeypatch,
    changes_history,
):
    messages = [{"role": "user", "content": "trace truth"}]
    events = []
    monkeypatch.setattr(agent_loop, "assemble_tool_pool", lambda *args: ([], {}))
    monkeypatch.setattr(
        agent_loop,
        "assemble_system_prompt",
        lambda *args: "s" * 310_000,
    )
    monkeypatch.setattr(agent_loop, "update_context", lambda *args: {})
    monkeypatch.setattr(
        agent_loop,
        "compact_history",
        (
            lambda current, **kwargs: [
                {"role": "user", "content": "[Context checkpoint]\nnew"}
            ]
            if changes_history else current
        ),
    )
    monkeypatch.setattr(
        agent_loop,
        "record_event",
        lambda event_type, **payload: events.append({
            "type": event_type,
            **payload,
        }),
    )

    agent_loop.prepare_context(messages)

    event = next(
        item for item in events
        if item["type"] == "context_compact"
        and item["stage"] == "compact_history"
    )
    assert event["changed"] is changes_history
    assert any(
        item["type"] == "context_integrity" for item in events
    ) is changes_history


def test_prepare_context_below_trigger_does_not_report_compact(monkeypatch):
    messages = [{"role": "user", "content": "small"}]
    events = []
    monkeypatch.setattr(agent_loop, "assemble_tool_pool", lambda *args: ([], {}))
    monkeypatch.setattr(
        agent_loop, "assemble_system_prompt", lambda *args: "system"
    )
    monkeypatch.setattr(agent_loop, "update_context", lambda *args: {})
    monkeypatch.setattr(
        agent_loop,
        "compact_history",
        lambda *args, **kwargs: pytest.fail("must remain below trigger"),
    )
    monkeypatch.setattr(
        agent_loop,
        "record_event",
        lambda event_type, **payload: events.append({
            "type": event_type,
            **payload,
        }),
    )

    agent_loop.prepare_context(messages)

    assert not any(
        event["type"] == "context_compact" for event in events
    )


@pytest.mark.parametrize(
    "summary_behavior",
    [
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("offline")
        ),
        lambda *_args, **_kwargs: " ",
    ],
)
def test_summary_failure_keeps_placeholder_but_not_full_large_result(
    tmp_path,
    monkeypatch,
    summary_behavior,
):
    runtime = make_runtime(tmp_path)
    huge = "Q" * (
        compact.MAX_TOOL_RESULT_TOKENS
        * compact.ESTIMATED_CHARS_PER_TOKEN
        + 3
    )
    messages = [
        {"role": "user", "content": "Keep ordinary history."},
        *exchange(0, huge),
    ]
    for index in range(1, 7):
        messages.extend(exchange(index, "normal"))
    monkeypatch.setattr(compact, "summarize_history", summary_behavior)

    result = force_compact(messages, runtime=runtime)

    assert "Keep ordinary history." in render(result)
    assert huge not in render(result)
    assert "[Large tool result omitted]" in render(result)
    assert_tool_pairs(result)
    assert runtime.state.metadata.get("compact_generation", 0) == 0


def test_normal_recent_tool_result_is_not_modified(monkeypatch):
    install_summary(monkeypatch)
    messages = long_history(8, width=100)
    recent = exchange(50, "precise recent output")
    messages.extend(recent)

    result = force_compact(messages)

    assert result[-2:] == recent


def test_recent_tool_exchanges_obey_total_tail_budget(monkeypatch):
    calls = install_summary(monkeypatch, "bounded")
    messages = [{"role": "user", "content": "Keep budget bounded."}]
    for index in range(6):
        messages.extend(exchange(
            index,
            str(index) * (
                7_000 * compact.ESTIMATED_CHARS_PER_TOKEN
            ),
        ))

    result = force_compact(
        messages,
        target_context_budget=120_000,
    )

    retained_results = [
        block
        for _, _, block in compact.collect_tool_results(result)
    ]
    assert len(calls) == 1
    assert len(retained_results) == 2
    assert [block["tool_use_id"] for block in retained_results] == [
        "tool-4",
        "tool-5",
    ]
    assert "tool-3" in render(calls[0])
    assert (
        compact.estimate_context_tokens(compact.estimate_size(result[2:]))
        <= compact.RECENT_TAIL_MAX_TOKENS
    )
    assert_tool_pairs(result)


def test_near_limit_tool_result_allows_compact_to_progress(monkeypatch):
    calls = install_summary(monkeypatch, "bounded")
    latest = {
        "role": "user",
        "content": "Preserve this instruction with the large result.",
    }
    messages = [latest]
    for index in range(5):
        messages.extend(exchange(index, f"small-{index}"))
    near_limit = "N" * (
        (compact.MAX_TOOL_RESULT_TOKENS - 100)
        * compact.ESTIMATED_CHARS_PER_TOKEN
    )
    messages.extend(exchange(99, near_limit))

    result = force_compact(
        messages,
        target_context_budget=50_000,
    )

    assert len(calls) == 1
    assert result[1] == latest
    assert near_limit in render(result)
    assert "[Large tool result omitted]" not in render(result)
    assert compact.estimate_size(result) <= 50_000
    assert_tool_pairs(result)


def test_candidate_budget_expands_prefix_before_summary(monkeypatch):
    calls = install_summary(monkeypatch, "bounded")
    messages = [{"role": "user", "content": "Keep candidate bounded."}]
    for index in range(6):
        messages.extend(exchange(index, str(index) * 7_000))

    def assembled_size(candidate):
        return compact.estimate_size(candidate) + 25_000

    result = force_compact(
        messages,
        target_context_budget=52_000,
        request_size_fn=assembled_size,
    )

    assert len(calls) == 1
    assert assembled_size(result) <= 42_500
    assert len(compact.collect_tool_results(result)) == 1
    assert_tool_pairs(result)


@pytest.mark.parametrize(
    "summary_behavior",
    [
        lambda _messages, runtime=None: (_ for _ in ()).throw(
            RuntimeError("offline")
        ),
        lambda _messages, runtime=None: "   ",
        lambda _messages, runtime=None: "s" * 130_000,
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


def test_summary_input_over_256k_fails_before_model_call_and_is_not_retried(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    events = []
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: pytest.fail("unsafe request must not be sent"),
    )
    monkeypatch.setattr(
        compact,
        "record_event",
        lambda event_type, **payload: events.append({
            "type": event_type,
            **payload,
        }),
    )
    oversized_reasoning = "r" * (
        260_000 * compact.ESTIMATED_CHARS_PER_TOKEN
    )
    oversized_exchange = exchange(0, "result")
    oversized_exchange[0]["reasoning_content"] = oversized_reasoning
    messages = [
        {"role": "user", "content": "Keep this latest request exact."},
        *oversized_exchange,
    ]
    for index in range(1, 6):
        messages.extend(exchange(index, "recent"))
    original = json.loads(json.dumps(messages))

    first = compact.compact_history(
        messages,
        runtime=runtime,
        allow_model_summary=True,
        request_size_fn=compact.estimate_size,
    )
    second = compact.compact_history(
        first,
        runtime=runtime,
        allow_model_summary=True,
        request_size_fn=compact.estimate_size,
    )

    assert first is second is messages
    assert messages == original
    failures = [
        event["failure_reason"]
        for event in events
        if event["type"] == "compact" and not event["success"]
    ]
    assert "SUMMARY_INPUT_LIMIT_TOKENS=256000" in failures[0]
    assert failures[1] == "unchanged history matches the last failed compact"


def test_large_reasoning_tool_exchange_uses_256k_summary_input_window(
    monkeypatch,
):
    calls = install_summary(monkeypatch, "large exchange retained")
    latest = {
        "role": "user",
        "content": "Keep this latest instruction outside the summary.",
    }
    large_reasoning = "r" * (
        140_000 * compact.ESTIMATED_CHARS_PER_TOKEN
    )
    large_exchange = exchange(0, "tool result remains paired")
    large_exchange[0]["reasoning_content"] = large_reasoning
    messages = [latest, *large_exchange]
    for index in range(1, 6):
        messages.extend(exchange(index, "recent"))

    result = compact.compact_history(
        messages,
        allow_model_summary=True,
        request_size_fn=compact.estimate_size,
        system="SYSTEM_PROMPT_MUST_NOT_BE_SUMMARIZED",
    )

    assert len(calls) == 1
    summary_input = calls[0]
    assert large_reasoning in render(summary_input)
    assert latest not in summary_input
    assert "SYSTEM_PROMPT_MUST_NOT_BE_SUMMARIZED" not in render(summary_input)
    assert_tool_pairs(summary_input)
    assert result[1] == latest
    assert checkpoint_count(result) == 1


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


def test_unchanged_failed_automatic_compact_is_not_retried(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    calls = []

    def fail(messages, runtime=None):
        calls.append(render(messages))
        raise RuntimeError("offline")

    monkeypatch.setattr(compact, "summarize_history", fail)
    messages = long_history(10)
    sizer = lambda candidate: compact.estimate_size(candidate) + 310_000

    first = compact.compact_history(
        messages,
        runtime=runtime,
        allow_model_summary=True,
        target_context_budget=350_000,
        request_size_fn=sizer,
    )
    second = compact.compact_history(
        first,
        runtime=runtime,
        allow_model_summary=True,
        target_context_budget=350_000,
        request_size_fn=sizer,
    )
    changed = [*second, {"role": "assistant", "content": "new history"}]
    compact.compact_history(
        changed,
        runtime=runtime,
        allow_model_summary=True,
        target_context_budget=350_000,
        request_size_fn=sizer,
    )

    assert len(calls) == 2
    assert runtime.state.metadata["last_failed_compact_signature"]
    assert len(runtime.state.metadata["last_failed_compact_signature"]) == 64
    assert first == second == messages


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
        target_context_budget=30_000,
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

    assert not (runtime.paths.state_root / ".aqours_code" / "context-archives").exists()


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


def test_latest_user_instruction_survives_three_compacts_verbatim(
    monkeypatch,
):
    exact = "DO NOT CHANGE THIS EXACT USER REQUIREMENT: αβγ"
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: "summary deliberately omits the requirement",
    )
    messages = [{"role": "user", "content": exact}]
    for generation in range(3):
        for index in range(generation * 6, generation * 6 + 6):
            messages.extend(exchange(index, "work-" + "x" * 300))
        messages = force_compact(messages)
        assert messages[1] == {"role": "user", "content": exact}
        assert sum(
            message == {"role": "user", "content": exact}
            for message in messages
        ) == 1
        assert checkpoint_count(messages) == 1
        assert not hasattr(compact, "LATEST_USER_INSTRUCTION_MARKER")
        assert not hasattr(compact, "LATEST_USER_INSTRUCTION_END_MARKER")


def test_new_real_user_instruction_replaces_prior_latest_instruction(
    monkeypatch,
):
    old = "OLD REQUIREMENT"
    new = "NEW REQUIREMENT"
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: "summary omits both requirements",
    )
    messages = [{"role": "user", "content": old}, *long_history(6)[1:]]
    messages = force_compact(messages)
    messages.append({"role": "user", "content": new})
    for index in range(10, 16):
        messages.extend(exchange(index, "new work-" + "y" * 300))

    messages = force_compact(messages)

    assert messages[1] == {"role": "user", "content": new}
    assert old not in render(messages)
    assert sum(
        message == {"role": "user", "content": new}
        for message in messages
    ) == 1


def test_background_notification_does_not_replace_latest_user_instruction(
    monkeypatch,
):
    exact = "REAL USER REQUIREMENT"
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: "summary",
    )
    messages = [
        {"role": "user", "content": exact},
        {
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    "<task_notification><status>completed</status>"
                    "</task_notification>"
                ),
            }],
        },
    ]
    for index in range(6):
        messages.extend(exchange(index, "work"))

    result = force_compact(messages)

    assert result[1] == {"role": "user", "content": exact}
    assert sum(
        message == {"role": "user", "content": exact}
        for message in result
    ) == 1


def test_list_form_instruction_and_multiple_result_blocks_are_supported(
    monkeypatch,
):
    exact = "LIST BLOCK REQUIREMENT"
    huge = "Z" * (
        compact.MAX_TOOL_RESULT_TOKENS
        * compact.ESTIMATED_CHARS_PER_TOKEN
        + 3
    )
    multi_use = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "large", "name": "read_file",
             "input": {"path": "large.py"}},
            {"type": "tool_use", "id": "small", "name": "read_file",
             "input": {"path": "small.py"}},
        ],
    }
    multi_result = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "large",
             "content": huge},
            {"type": "tool_result", "tool_use_id": "small",
             "content": "small exact"},
        ],
    }
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: "summary",
    )
    messages = [
        {"role": "user", "content": [{"type": "text", "text": exact}]},
        multi_use,
        multi_result,
    ]
    for index in range(6):
        messages.extend(exchange(index, "work"))

    sanitized, count = compact.sanitize_context_tool_results(messages)
    result = force_compact(messages)

    assert count == 1
    sanitized_results = compact.collect_tool_results(sanitized)
    assert sanitized_results[0][2]["tool_use_id"] == "large"
    assert sanitized_results[0][2]["content"].startswith(
        "[Large tool result omitted]"
    )
    assert sanitized_results[1][2]["content"] == "small exact"
    assert exact in render(result)
    assert huge not in render(result)
    assert_tool_pairs(result)
