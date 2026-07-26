import json
from types import SimpleNamespace

import pytest

from codepilot_s20 import agent_loop, basic_tools, compact, context, prompts
from codepilot_s20.command_executor import LocalCommandExecutor
from codepilot_s20.knowledge import ReadObservation, content_digest
from codepilot_s20.runtime import AgentRuntime
from codepilot_s20.semantic_memory import (
    SEMANTIC_MEMORY_PROMPT_LIMIT,
    empty_semantic_delta,
)


def tool_exchange(name: str, contents: list[str], *,
                  tool_name: str = "read_file",
                  paths: list[str] | None = None):
    ids = [f"{name}_{index}" for index in range(len(contents))]
    paths = paths or [f"{tool_id}.py" for tool_id in ids]
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id,
             "name": tool_name,
             "input": ({"path": path} if tool_name == "read_file" else {})}
            for tool_id, path in zip(ids, paths)
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id,
             "content": content}
            for tool_id, content in zip(ids, contents)
        ]},
    ]


def result_contents(message):
    return [block["content"] for block in message["content"]
            if block.get("type") == "tool_result"]


def structured_payload(*, summary="checkpoint", delta=None):
    return {
        "conversation_checkpoint": {
            "summary": summary,
            "current_focus": "continue implementation",
            "remaining": ["run tests"],
        },
        "semantic_memory_delta": delta or empty_semantic_delta(),
    }


def acknowledged_payload(history, *, delta=None, summary="checkpoint"):
    payload_delta = delta or empty_semantic_delta()
    payload_delta["processed_tool_use_ids"] = [
        str(block.get("tool_use_id"))
        for _, _, block in compact.collect_tool_results(history)
        if block.get("tool_use_id")
    ]
    return structured_payload(summary=summary, delta=payload_delta)


def semantic_file_card(
    path: str,
    source_id: str,
    *,
    purpose: str = "",
    conclusions=None,
    supersede_fields=None,
):
    return {
        "path": path,
        "digest": "model-controlled-wrong-digest",
        "stale": False,
        "source_tool_use_ids": [source_id],
        "supersede_fields": list(supersede_fields or []),
        "purpose": purpose,
        "key_symbols": [],
        "key_behaviors": [],
        "important_conditions": [],
        "relationships": [],
        "relevant_ranges": [],
        "short_snippets": [],
        "conclusions": list(conclusions or []),
        "uncertainties": [],
    }


def make_runtime(tmp_path, model_client=None):
    return AgentRuntime.create(
        workdir=tmp_path,
        model_client=model_client or SimpleNamespace(messages=object()),
        command_executor=LocalCommandExecutor(),
        model_provider="test",
        model="test",
        root_task="Preserve semantics after compact",
    )


def test_micro_compact_does_not_touch_eight_result_batch_below_trigger():
    contents = [f"file {index}\n" + "x" * 700 for index in range(8)]
    messages = [{"role": "user", "content": "inspect repository"},
                *tool_exchange("latest", contents)]

    compact.micro_compact(messages)

    assert result_contents(messages[-1]) == contents
    assert compact.estimate_size(messages) < compact.MICRO_COMPACT_TRIGGER


def test_prepare_context_preserves_eight_result_batch_below_trigger():
    contents = [f"file {index}\n" + "x" * 700 for index in range(8)]
    messages = [{"role": "user", "content": "inspect repository"},
                *tool_exchange("pipeline", contents)]

    agent_loop.prepare_context(messages)

    assert result_contents(messages[-1]) == contents


def test_prepare_context_budget_includes_system_and_tool_schemas(monkeypatch):
    messages = [{"role": "user", "content": "small message"}]
    monkeypatch.setattr(agent_loop, "CONTEXT_LIMIT", 500)
    monkeypatch.setattr(
        agent_loop, "update_context", lambda context, messages: {},
    )
    monkeypatch.setattr(
        agent_loop, "assemble_system_prompt",
        lambda context: "system-" + ("s" * 500),
    )
    monkeypatch.setattr(
        agent_loop, "assemble_tool_pool",
        lambda: ([{"name": "read_file", "schema": "x" * 500}], {}),
    )
    monkeypatch.setattr(
        agent_loop,
        "compact_history",
        lambda value, **kwargs: [{"role": "user", "content": "compacted"}],
    )

    agent_loop.prepare_context(messages)

    assert messages == [{"role": "user", "content": "compacted"}]


def test_micro_compact_preserves_recent_distinct_read_working_set():
    old = "old evidence\n" + "o" * 13000
    recent = "recent evidence\n" + "r" * 12000
    latest = "latest evidence\n" + "l" * 12000
    messages = [{"role": "user", "content": "complex task"},
                *tool_exchange("old", [old]),
                *tool_exchange("recent", [recent]),
                *tool_exchange("latest", [latest])]

    compact.micro_compact(messages)

    assert result_contents(messages[2]) == [old]
    assert result_contents(messages[4]) == [recent]
    assert result_contents(messages[6]) == [latest]


def test_unique_read_results_are_not_deleted_before_semantic_compact():
    contents = [f"evidence {index}\n" + str(index) * 1000
                for index in range(4)]
    messages = [{"role": "user", "content": "complex task"}]
    for index, content in enumerate(contents):
        messages.extend(tool_exchange(f"batch_{index}", [content]))

    compact.micro_compact(messages, trigger_size=1, target_size=1)

    assert [
        result_contents(messages[2 + index * 2])[0]
        for index in range(4)
    ] == contents


def test_run_knowledge_expands_protection_for_a_wide_valid_working_set(
    tmp_path, monkeypatch,
):
    runtime = AgentRuntime.create(
        workdir=tmp_path,
        model_client=SimpleNamespace(messages=object()),
        command_executor=LocalCommandExecutor(),
        model_provider="test",
        model="test",
    )
    messages = [{"role": "user", "content": "inspect wide repository"}]
    for index in range(18):
        path = f"src/module_{index}.py"
        content = f"source {index}\n" + ("x" * 300)
        runtime.state.knowledge.observe_file(
            path, f"def symbol_{index}():\n    return {index}\n",
        )
        messages.extend(tool_exchange(
            f"read_{index}", [content], paths=[path],
        ))
    compact.micro_compact(
        messages, trigger_size=1, target_size=1, runtime=runtime,
    )

    assert all(
        result_contents(messages[2 + index * 2]) == [
            f"source {index}\n" + ("x" * 300)
        ]
        for index in range(18)
    )


def test_wide_source_batch_survives_narrow_followup_reads():
    source_paths = [f"src/package/module_{index}.py" for index in range(12)]
    source_contents = [f"source {index}\n" + chr(97 + index) * 900
                       for index in range(12)]
    test_paths = [f"tests/test_{index}.py" for index in range(3)]
    test_contents = [f"test {index}\n" + str(index) * 700
                     for index in range(3)]
    messages = [
        {"role": "user", "content": "complex repository task"},
        *tool_exchange("sources", source_contents, paths=source_paths),
        *tool_exchange("tests", test_contents, paths=test_paths),
        *tool_exchange("followup", [source_contents[5]],
                       paths=[source_paths[5]]),
    ]

    compact.micro_compact(messages, trigger_size=1, target_size=1)

    retained_sources = result_contents(messages[2])
    assert retained_sources[0] == source_contents[0]
    assert retained_sources[11] == source_contents[11]
    assert "Duplicate read compacted" in retained_sources[5]
    assert result_contents(messages[4]) == test_contents
    assert result_contents(messages[6]) == [source_contents[5]]


def test_todo_and_reminder_do_not_delete_unique_working_batches():
    old = "old evidence\n" + "o" * 1000
    recent = "recent evidence\n" + "r" * 1000
    todo = "todo updated\n" + "t" * 300
    latest = "latest evidence\n" + "l" * 1000
    messages = [
        {"role": "user", "content": "complex task"},
        *tool_exchange("old", [old]),
        *tool_exchange("recent", [recent]),
        *tool_exchange("todo", [todo], tool_name="todo_write"),
        {"role": "user", "content": "<reminder>Update your todos.</reminder>"},
        *tool_exchange("latest", [latest]),
    ]

    compact.micro_compact(messages, trigger_size=1, target_size=1)

    assert result_contents(messages[2]) == [old]
    assert result_contents(messages[4]) == [recent]
    assert result_contents(messages[9]) == [latest]


def test_micro_compact_prefers_identical_path_and_content_duplicates():
    independent = "independent evidence\n" + "i" * 1000
    duplicate = "same file evidence\n" + "d" * 1000
    latest = "latest evidence\n" + "l" * 700
    messages = [
        {"role": "user", "content": "complex task"},
        *tool_exchange("independent", [independent]),
        *tool_exchange(
            "duplicate_old", [duplicate], paths=["/workspace/src/item.py"]),
        *tool_exchange(
            "duplicate_new", [duplicate], paths=["/workspace/src/item.py"]),
        *tool_exchange("latest", [latest]),
    ]
    target = compact.estimate_size(messages) - 500

    compact.micro_compact(messages, trigger_size=1, target_size=target)

    assert result_contents(messages[2]) == [independent]
    assert "Duplicate read compacted" in result_contents(messages[4])[0]
    assert result_contents(messages[6]) == [duplicate]
    assert result_contents(messages[8]) == [latest]


def test_same_path_with_different_content_is_not_deduplicated():
    independent = "independent evidence\n" + "i" * 1000
    old_version = "old file version\n" + "a" * 1000
    new_version = "new file version\n" + "b" * 1000
    latest = "latest evidence\n" + "l" * 700
    messages = [
        {"role": "user", "content": "complex task"},
        *tool_exchange("independent", [independent]),
        *tool_exchange(
            "version_old", [old_version], paths=["/workspace/src/item.py"]),
        *tool_exchange(
            "version_new", [new_version], paths=["/workspace/src/item.py"]),
        *tool_exchange("latest", [latest]),
    ]
    target = compact.estimate_size(messages) - 500

    compact.micro_compact(messages, trigger_size=1, target_size=target)

    assert result_contents(messages[2]) == [independent]
    assert result_contents(messages[4]) == [old_version]
    assert result_contents(messages[6]) == [new_version]


def test_tool_result_budget_persists_but_keeps_unique_results_for_extraction(
        tmp_path, monkeypatch):
    monkeypatch.setattr(compact, "TOOL_RESULTS_DIR", tmp_path / "tool-results")
    contents = [
        (f"result {index}\n" + str(index) * 32000)
        for index in range(4)
    ]
    messages = [{"role": "user", "content": "inspect"},
                *tool_exchange("wide", contents),
                {"role": "user", "content": "<reminder>Update todos.</reminder>"}]

    compact.tool_result_budget(messages)

    budgeted = result_contents(messages[-2])
    assert budgeted == contents
    for index, original in enumerate(contents):
        path = tmp_path / "tool-results" / f"wide_{index}.txt"
        assert path.read_text(encoding="utf-8") == original


def test_snip_compact_keeps_small_histories_even_above_message_count():
    messages = [{"role": "user", "content": f"short {index}"}
                for index in range(60)]

    result = compact.snip_compact(messages)

    assert result == messages
    assert len(result) == 60


def test_compact_history_keeps_recent_tool_exchange_paired(tmp_path, monkeypatch):
    monkeypatch.setattr(compact, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda messages: acknowledged_payload(messages),
    )
    messages = [
        {"role": "user", "content": f"history {index}"}
        for index in range(4)
    ]
    messages.extend(tool_exchange("paired", ["important result"]))
    messages.extend([
        {"role": "assistant", "content": [{"type": "text", "text": "reason"}]},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": [{"type": "text", "text": "next"}]},
        {"role": "user", "content": "more"},
    ])

    result = compact.compact_history(messages)

    assert result[0]["content"].startswith("[Compacted checkpoint]")
    assert '"summary":"checkpoint"' in result[0]["content"]
    paired_use = next(index for index, message in enumerate(result)
                      if message.get("role") == "assistant"
                      and compact.message_has_tool_use(message))
    assert compact.is_tool_result_message(result[paired_use + 1])


def test_summary_prompt_is_structured_generic_and_uses_complete_messages(monkeypatch):
    captured = {}
    trace_calls = []

    class Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(structured_payload()),
                )
            ])

    monkeypatch.setattr(
        compact, "client", SimpleNamespace(messages=Messages()))
    monkeypatch.setattr(
        compact, "record_llm_request",
        lambda **fields: trace_calls.append(("request", fields)),
    )
    monkeypatch.setattr(
        compact, "record_llm_response",
        lambda response, **fields: trace_calls.append(("response", fields)),
    )

    unique = "middle-only semantic fact"
    result = compact.summarize_history(
        [{"role": "user", "content": unique}],
    )

    prompt = captured["messages"][0]["content"]
    assert result["conversation_checkpoint"]["summary"] == "checkpoint"
    assert unique in prompt
    assert "semantic_memory_delta" in prompt
    assert "cross-file calls/data relationships" in prompt
    assert "any/all/fingerprint/normalized" not in prompt
    assert trace_calls == [
        ("request", {
            "model": compact.MODEL,
            "max_tokens": 2000,
            "message_count": 1,
            "tool_count": 0,
            "purpose": "compact_summary",
            "agent_role": "",
        }),
        ("response", {"purpose": "compact_summary", "agent_role": ""}),
    ]


def test_compact_uses_deterministic_checkpoint_in_finalization_reserve(
    tmp_path, monkeypatch,
):
    class BudgetedClient:
        def __init__(self):
            self.messages = self
            self.calls = []

        def budget_snapshot(self):
            return {
                "max_calls": 40,
                "call_count": 34,
                "max_provider_retries": 1,
            }

        def create(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("model summary must not consume the tail reserve")

    client = BudgetedClient()
    monkeypatch.setattr(compact, "client", client)
    monkeypatch.setattr(compact, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(compact, "CURRENT_ROOT_TASK", "Fix the contract")
    monkeypatch.setattr(compact, "CURRENT_TODOS", [{
        "content": "Quantity is fingerprinted",
        "status": "in_progress",
        "kind": "acceptance",
    }])
    messages = [
        {"role": "user", "content": "old context " + ("x" * 1000)}
        for _ in range(8)
    ]

    result = compact.compact_history(messages)

    assert client.calls == []
    raw_contents = [
        item["content"] for item in result
        if isinstance(item.get("content"), str)
        and item["content"].startswith("old context ")
    ]
    assert raw_contents == [item["content"] for item in messages]
    checkpoint = next(
        item["content"] for item in result
        if isinstance(item.get("content"), str)
        and item["content"].startswith("[Compacted checkpoint]")
    )
    assert "finalization-call reserve" in checkpoint
    assert "Quantity is fingerprinted" in checkpoint


def test_middle_fact_enters_semantic_memory_before_removal(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    unique = "ROLE: routes reservations to inventory adapter"
    messages = [{"role": "user", "content": "hard constraint: preserve API"}]
    messages.extend(tool_exchange(
        "middle",
        [unique + "\nRELATIONSHIP: api -> service -> store"],
        paths=["src/service.py"],
    ))
    messages.extend(
        {"role": "user", "content": f"later {index}"}
        for index in range(8)
    )
    captured = {}

    def summarize(history, runtime_arg):
        captured["history"] = history
        delta = empty_semantic_delta()
        delta["task"]["constraints"] = ["preserve API"]
        delta["files"] = [{
            "path": "src/service.py",
            "digest": "d1",
            "stale": False,
            "purpose": "routes reservations",
            "key_symbols": [],
            "key_behaviors": ["delegates inventory reservation"],
            "important_conditions": [],
            "relationships": ["api -> service -> store"],
            "relevant_ranges": [],
            "short_snippets": [unique],
            "conclusions": [],
            "uncertainties": [],
        }]
        return acknowledged_payload(history, delta=delta)

    monkeypatch.setattr(compact, "summarize_history", summarize)
    result = compact.compact_history(
        messages, allow_model_summary=True, runtime=runtime,
    )

    assert unique in json.dumps(captured["history"])
    assert unique not in json.dumps(result)
    memory = runtime.state.semantic_memory.as_dict()
    assert memory["task"]["constraints"] == ["preserve API"]
    assert memory["files"][0]["relationships"] == [
        "api -> service -> store"
    ]


def test_full_compact_model_sees_unmarked_unique_tool_result(
    tmp_path, monkeypatch,
):
    runtime = make_runtime(tmp_path)
    unique = "UNIQUE TOOL RESULT: semaphore guards the commit boundary"
    messages = [{"role": "user", "content": "inspect"}]
    messages.extend(tool_exchange(
        "raw", [unique + (" x" * 500)], paths=["src/lock.py"],
    ))
    messages.extend(
        {"role": "user", "content": f"tail {index}"}
        for index in range(7)
    )
    captured = {}

    def summarize(history, runtime_arg):
        captured["serialized"] = json.dumps(history)
        return acknowledged_payload(history)

    monkeypatch.setattr(compact, "summarize_history", summarize)
    compact.micro_compact(messages, trigger_size=1, target_size=1)
    compact.compact_history(
        messages, allow_model_summary=True, runtime=runtime,
    )

    assert unique in captured["serialized"]
    assert "Earlier tool result compacted" not in captured["serialized"]


def test_repeated_compacts_merge_canonical_semantics(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    deltas = []
    for index in range(3):
        delta = empty_semantic_delta()
        if index == 0:
            delta["task"]["constraints"] = ["do not change public API"]
            delta["files"] = [{
                "path": "src/early.py", "digest": "early",
                "stale": False, "purpose": "early coordinator",
                "key_symbols": [], "key_behaviors": ["dispatches work"],
                "important_conditions": [], "relationships": [],
                "relevant_ranges": [], "short_snippets": [],
                "conclusions": [], "uncertainties": [],
            }]
        delta["decisions"] = [f"decision-{index}"]
        deltas.append(delta)

    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda history, runtime_arg: structured_payload(
            delta=deltas.pop(0),
        ),
    )
    messages = [{"role": "user", "content": f"old {i}"} for i in range(8)]
    for round_index in range(3):
        messages = compact.compact_history(
            messages, allow_model_summary=True, runtime=runtime,
        )
        messages.extend(
            {"role": "user", "content": f"round {round_index} new {i}"}
            for i in range(6)
        )

    memory = runtime.state.semantic_memory.as_dict()
    assert memory["task"]["constraints"] == ["do not change public API"]
    assert memory["files"][0]["purpose"] == "early coordinator"
    assert memory["decisions"] == [
        "decision-0", "decision-1", "decision-2",
    ]


def test_same_path_and_digest_semantic_observations_merge(tmp_path):
    runtime = make_runtime(tmp_path)
    observation = ReadObservation(
        tool_use_id="read-item",
        path="src/item.py",
        digest="same",
        offset=0,
        limit=None,
        range_start=0,
        range_end=1,
        total_lines=1,
    )
    first = empty_semantic_delta()
    first["files"] = [{
        "path": "./src/item.py", "digest": "same", "stale": False,
        "source_tool_use_ids": ["read-item"],
        "purpose": "stores items", "key_symbols": ["Item"],
        "key_behaviors": [], "important_conditions": [],
        "relationships": [], "relevant_ranges": [], "short_snippets": [],
        "conclusions": [], "uncertainties": [],
    }]
    second = empty_semantic_delta()
    second["files"] = [{
        "path": "src\\item.py", "digest": "same", "stale": False,
        "source_tool_use_ids": ["read-item"],
        "purpose": "", "key_symbols": [],
        "key_behaviors": ["rejects duplicate ids"],
        "important_conditions": [], "relationships": [],
        "relevant_ranges": [], "short_snippets": [],
        "conclusions": [], "uncertainties": [],
    }]

    runtime.state.semantic_memory.merge(
        first,
        observations={"read-item": observation},
        current_digests={"src/item.py": "same"},
    )
    runtime.state.semantic_memory.merge(
        second,
        observations={"read-item": observation},
        current_digests={"src/item.py": "same"},
    )

    files = runtime.state.semantic_memory.as_dict()["files"]
    assert len(files) == 1
    assert files[0]["path"] == "src/item.py"
    assert files[0]["purpose"] == "stores items"
    assert files[0]["key_behaviors"] == ["rejects duplicate ids"]


def test_semantic_prompt_and_raw_tail_remain_bounded_for_many_files(
    tmp_path, monkeypatch,
):
    runtime = make_runtime(tmp_path)
    delta = empty_semantic_delta()
    delta["files"] = [{
        "path": f"src/file_{index}.py",
        "digest": f"digest-{index}",
        "stale": False,
        "purpose": "p" * 200,
        "key_symbols": [f"symbol_{index}"],
        "key_behaviors": ["behavior " + ("b" * 200)],
        "important_conditions": ["condition " + ("c" * 200)],
        "relationships": ["relationship " + ("r" * 200)],
        "relevant_ranges": ["1:20"],
        "short_snippets": ["snippet " + ("s" * 400)],
        "conclusions": [],
        "uncertainties": [],
    } for index in range(100)]
    monkeypatch.setattr(
        compact, "summarize_history",
        lambda history, runtime_arg: acknowledged_payload(
            history, delta=delta,
        ),
    )
    messages = [{"role": "user", "content": "inspect 100 files"}]
    for index in range(100):
        messages.extend(tool_exchange(
            f"read_{index}",
            [f"content for file {index}"],
            paths=[f"src/file_{index}.py"],
        ))
    result = compact.compact_history(
        messages, allow_model_summary=True, runtime=runtime,
    )
    view = runtime.state.semantic_memory.prompt_view()

    assert len(view) <= SEMANTIC_MEMORY_PROMPT_LIMIT
    assert len(runtime.state.semantic_memory.files) <= 24
    assert len(result) <= compact.COMPACT_KEEP_TAIL_MESSAGES + 2


def test_invalid_compact_json_uses_deterministic_fallback(
    tmp_path, monkeypatch,
):
    class InvalidClient:
        def __init__(self):
            self.messages = self
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(content=[
                SimpleNamespace(type="text", text="not-json")
            ])

    client = InvalidClient()
    runtime = make_runtime(tmp_path, client)
    messages = [
        {"role": "user", "content": "preserve behavior"},
        *tool_exchange(
            "fallback",
            ["ROLE: validates requests\nCONDITION: reject negatives"],
            paths=["src/validator.py"],
        ),
    ]

    payload = compact.summarize_history(messages, runtime)

    assert client.calls in {1, 2}
    assert payload["semantic_memory_delta"]["files"][0]["purpose"] == (
        "validates requests"
    )
    assert payload["semantic_memory_delta"]["files"][0][
        "important_conditions"
    ] == ["reject negatives"]


def test_fake_model_continues_from_semantics_after_tools_are_disabled(
    tmp_path, monkeypatch,
):
    class FakeMessages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("tools") == []:
                return SimpleNamespace(content=[
                    SimpleNamespace(type="text", text="continued")
                ])
            return SimpleNamespace(content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(structured_payload()),
                )
            ])

    fake_messages = FakeMessages()
    runtime = make_runtime(
        tmp_path, SimpleNamespace(messages=fake_messages),
    )
    messages = [{"role": "user", "content": "implement reservation flow"}]
    for index in range(10):
        messages.extend(tool_exchange(
            f"f{index}",
            [f"ROLE: layer {index}\nCONDITION: invariant {index}\n"
             f"RELATIONSHIP: layer {index} -> layer {index + 1}"],
            paths=[f"src/layer_{index}.py"],
        ))
    delta = compact._fallback_semantic_payload(
        messages, runtime,
    )["semantic_memory_delta"]
    monkeypatch.setattr(
        compact, "summarize_history",
        lambda history, runtime_arg: acknowledged_payload(
            history, delta=delta,
        ),
    )
    compacted = compact.compact_history(
        messages, allow_model_summary=True, runtime=runtime,
    )
    live = context.update_context({}, compacted, runtime)
    system = prompts.assemble_system_prompt(live, runtime)
    fake_messages.create(
        model="test", system=system, messages=compacted,
        tools=[], max_tokens=1000,
    )

    final_call = fake_messages.calls[-1]
    assert final_call["tools"] == []
    assert "layer 0" in final_call["system"]
    assert "invariant 0" in final_call["system"]
    assert "layer 0 -> layer 1" in final_call["system"]


def test_historical_read_uses_observation_digest_not_current_digest(
    tmp_path, monkeypatch,
):
    path = tmp_path / "a.py"
    path.write_text("version = 1\n", encoding="utf-8")
    runtime = make_runtime(tmp_path)
    runtime.state.semantic_memory.clear("goal A")
    v1_output = basic_tools.run_read(
        "a.py", runtime=runtime, _tool_use_id="read-v1",
    )
    v1_digest = runtime.state.read_observations["read-v1"].digest
    basic_tools.run_write("a.py", "version = 2\n", runtime=runtime)
    v2_digest = runtime.state.knowledge.files["a.py"].digest
    messages = tool_exchange(
        "read-v1", [v1_output], paths=["a.py"],
    )
    # tool_exchange generates read-v1_0; use the concrete observation id.
    messages[0]["content"][0]["id"] = "read-v1"
    messages[1]["content"][0]["tool_use_id"] = "read-v1"
    delta = empty_semantic_delta()
    delta["task"]["goal"] = "goal B"
    delta["files"] = [
        semantic_file_card(
            "wrong.py", "read-v1",
            conclusions=["semantics from v1"],
        )
    ]
    monkeypatch.setattr(
        compact, "summarize_history",
        lambda history, runtime_arg: acknowledged_payload(
            history, delta=delta,
        ),
    )

    compact.compact_history(
        messages, allow_model_summary=True, runtime=runtime,
    )

    card = runtime.state.semantic_memory.as_dict()["files"][0]
    assert runtime.state.semantic_memory.task["goal"] == "goal A"
    assert card["path"] == "a.py"
    assert card["digest"] == v1_digest
    assert card["digest"] != v2_digest
    assert card["stale"] is True

    v2_output = basic_tools.run_read(
        "a.py", runtime=runtime, _tool_use_id="read-v2",
    )
    v2_delta = empty_semantic_delta()
    v2_delta["files"] = [
        semantic_file_card(
            "a.py", "read-v2",
            conclusions=["semantics from v2"],
        )
    ]
    v2_messages = tool_exchange(
        "read-v2", [v2_output], paths=["a.py"],
    )
    v2_messages[0]["content"][0]["id"] = "read-v2"
    v2_messages[1]["content"][0]["tool_use_id"] = "read-v2"
    monkeypatch.setattr(
        compact, "summarize_history",
        lambda history, runtime_arg: acknowledged_payload(
            history, delta=v2_delta,
        ),
    )
    compact.compact_history(
        v2_messages, allow_model_summary=True, runtime=runtime,
    )

    cards = {
        card["digest"]: card
        for card in runtime.state.semantic_memory.as_dict()["files"]
    }
    assert cards[v1_digest]["conclusions"] == ["semantics from v1"]
    assert cards[v1_digest]["stale"] is True
    assert cards[v2_digest]["conclusions"] == ["semantics from v2"]
    assert cards[v2_digest]["stale"] is False


def test_dispatch_binds_read_observation_to_tool_result_id(tmp_path):
    (tmp_path / "bound.py").write_text("value = 1\n", encoding="utf-8")
    runtime = make_runtime(tmp_path)
    handler = lambda **kwargs: basic_tools.run_read(
        runtime=runtime, **kwargs,
    )

    output = basic_tools.call_tool_handler(
        handler,
        {"path": "bound.py", "offset": 0, "limit": 1},
        "read_file",
        tool_use_id="tool-read-1",
    )

    observation = runtime.state.read_observations["tool-read-1"]
    assert output == "value = 1"
    assert observation.path == "bound.py"
    assert observation.digest == content_digest(
        (tmp_path / "bound.py").read_bytes()
    )
    assert (observation.range_start, observation.range_end) == (0, 1)


def test_oversized_tool_results_transfer_middle_facts_before_raw_removal(
    tmp_path, monkeypatch,
):
    facts = [
        "UNIQUE_MIDDLE_FACT_ALPHA",
        "UNIQUE_MIDDLE_FACT_BETA",
    ]
    contents = [
        ("h" * 50000) + fact + ("t" * 50000)
        for fact in facts
    ]
    runtime = make_runtime(tmp_path)
    paths = []
    for index, content in enumerate(contents):
        path = f"large_{index}.txt"
        paths.append(path)
        (tmp_path / path).write_text(content, encoding="utf-8")
        basic_tools.run_read(
            path,
            runtime=runtime,
            _tool_use_id=f"large_{index}",
        )
    messages = tool_exchange("large", contents, paths=paths)
    compact.tool_result_budget(messages, runtime=runtime)
    assert result_contents(messages[1]) == contents
    seen_inputs = []

    def summarize(history, runtime_arg):
        text = json.dumps(history)
        seen_inputs.append(text)
        units = compact._history_units(history)
        assert all(
            len(unit) != 2
            or (
                compact.message_has_tool_use(unit[0])
                and compact.is_tool_result_message(unit[1])
            )
            for unit in units
        )
        delta = empty_semantic_delta()
        delta["files"] = [
            semantic_file_card(
                paths[index],
                f"large_{index}",
                conclusions=[fact],
            )
            for index, fact in enumerate(facts)
            if fact in text
        ]
        return acknowledged_payload(history, delta=delta)

    monkeypatch.setattr(compact, "summarize_history", summarize)
    result = compact.compact_history(
        messages, allow_model_summary=True, runtime=runtime,
    )

    assert all(any(fact in prompt for prompt in seen_inputs) for fact in facts)
    conclusions = {
        conclusion
        for card in runtime.state.semantic_memory.as_dict()["files"]
        for conclusion in card["conclusions"]
    }
    assert set(facts) <= conclusions
    assert not any(fact in json.dumps(result) for fact in facts)


def test_chunked_compact_meets_full_request_budget_and_preserves_all_facts(
    tmp_path, monkeypatch,
):
    runtime = make_runtime(tmp_path)
    facts = [f"CHUNK_FACT_{index}" for index in range(4)]
    messages = [
        {
            "role": "user",
            "content": ("x" * 30000) + fact + ("y" * 30000),
        }
        for fact in facts
    ]
    seen = []

    def summarize(history, runtime_arg):
        text = json.dumps(history)
        seen.append(history)
        delta = empty_semantic_delta()
        delta["decisions"] = [fact for fact in facts if fact in text]
        return structured_payload(delta=delta)

    monkeypatch.setattr(compact, "summarize_history", summarize)
    tools = [{"name": "wide", "input_schema": {"description": "t" * 5000}}]
    def sizer(candidate):
        live = context.update_context({}, candidate, runtime)
        system = prompts.assemble_system_prompt(live, runtime)
        return compact.estimate_context_size(
            candidate, system=system, tools=tools,
        )

    result = compact.compact_history(
        messages,
        allow_model_summary=True,
        runtime=runtime,
        target_context_budget=50000,
        request_size_fn=sizer,
    )

    assert sizer(result) <= 50000
    assert set(facts) <= set(runtime.state.semantic_memory.decisions)
    assert len(seen) >= 3


def test_compact_fails_diagnostically_when_fixed_context_exceeds_target(
    tmp_path, monkeypatch,
):
    runtime = make_runtime(tmp_path)
    calls = []

    def summarize(history, runtime_arg):
        calls.append(history)
        return structured_payload()

    monkeypatch.setattr(compact, "summarize_history", summarize)

    with pytest.raises(
        compact.ContextCompactionError,
        match="fixed system/tool/dynamic context|target budget",
    ):
        compact.reactive_compact(
            [{"role": "user", "content": "x" * 100000}],
            runtime,
            target_context_budget=50000,
            request_size_fn=lambda candidate: (
                60000 + compact.estimate_size(candidate)
            ),
        )

    assert len(calls) <= 2


def test_semantic_current_state_converges_and_file_conclusions_supersede():
    from codepilot_s20.semantic_memory import SessionSemanticMemory

    memory = SessionSemanticMemory()
    memory.clear("goal A")
    observation = ReadObservation(
        tool_use_id="read-a",
        path="a.py",
        digest="digest-a",
        offset=0,
        limit=None,
        range_start=0,
        range_end=10,
        total_lines=10,
    )
    first = empty_semantic_delta()
    first["task"]["goal"] = "goal B"
    first["progress"]["remaining"] = ["fix A"]
    first["open_questions"] = ["is A broken?"]
    first["next_actions"] = ["edit A"]
    first["files"] = [
        semantic_file_card(
            "a.py", "read-a", conclusions=["incorrect conclusion"],
        )
    ]
    memory.merge(
        first,
        observations={"read-a": observation},
        current_digests={"a.py": "digest-a"},
    )
    second = empty_semantic_delta()
    second["progress"]["completed"] = ["fix A"]
    second["progress"]["remaining"] = []
    second["open_questions"] = []
    second["next_actions"] = ["run tests"]
    second["files"] = [
        semantic_file_card(
            "a.py",
            "read-a",
            conclusions=["corrected conclusion"],
            supersede_fields=["conclusions"],
        )
    ]
    memory.merge(
        second,
        observations={"read-a": observation},
        current_digests={"a.py": "digest-a"},
    )
    state = memory.as_dict()

    assert state["task"]["goal"] == "goal A"
    assert state["progress"]["completed"] == ["fix A"]
    assert state["progress"]["remaining"] == []
    assert state["open_questions"] == []
    assert state["next_actions"] == ["run tests"]
    assert state["files"][0]["conclusions"] == ["corrected conclusion"]

    before = memory.as_dict()
    memory.merge(
        second,
        observations={"read-a": observation},
        current_digests={"a.py": "digest-a"},
    )
    assert memory.as_dict() == before


def test_file_card_without_read_observation_is_unknown_and_stale():
    from codepilot_s20.semantic_memory import SessionSemanticMemory

    memory = SessionSemanticMemory()
    delta = empty_semantic_delta()
    delta["files"] = [{
        **semantic_file_card(
            "a.py", "missing-read",
            conclusions=["model claims current content"],
        ),
        "digest": "current-digest",
        "stale": False,
    }]

    memory.merge(delta, current_digests={"a.py": "current-digest"})

    card = memory.as_dict()["files"][0]
    assert card["digest"] == "unknown"
    assert card["stale"] is True
    assert any(
        "could not be bound" in item
        for item in card["uncertainties"]
    )


def test_semantic_capacity_protects_focus_relevant_file():
    from codepilot_s20.semantic_memory import SessionSemanticMemory

    memory = SessionSemanticMemory()
    memory.clear("maintain important module")
    delta = empty_semantic_delta()
    delta["progress"]["current_focus"] = "finish src/important.py behavior"
    observations = {}
    current = {}
    cards = []
    for index in range(25):
        path = "src/important.py" if index == 0 else f"src/file_{index}.py"
        source_id = f"read-{index}"
        digest = f"digest-{index}"
        observations[source_id] = ReadObservation(
            tool_use_id=source_id,
            path=path,
            digest=digest,
            offset=0,
            limit=None,
            range_start=0,
            range_end=1,
            total_lines=1,
        )
        current[path] = digest
        cards.append(semantic_file_card(path, source_id))
    delta["files"] = cards

    memory.merge(
        delta,
        observations=observations,
        current_digests=current,
    )

    paths = {card["path"] for card in memory.as_dict()["files"]}
    assert len(paths) == 24
    assert "src/important.py" in paths


def test_acceptance_prompt_is_generic_without_benchmark_answer_terms():
    prompt = agent_loop._acceptance_review_message(
        [{"id": "a", "status": "pending", "content": "contract"}],
        {"service.py"},
        4,
    ).lower()

    for leaked in (
        "any/different/unchanged",
        "fingerprint",
        "normalized key",
        "any/all requirements",
    ):
        assert leaked not in prompt
    assert "producer chain for derived values" in prompt
    assert "state branches" in prompt
    assert "exceptional paths" in prompt
    assert "required-field completeness" in prompt
