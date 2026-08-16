import copy
import json
from types import SimpleNamespace

from aqours_code import agent_loop
from evals import run_eval


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, tool_input: dict | None = None, block_id: str = "call_1"):
    return SimpleNamespace(
        type="tool_use",
        name=name,
        input=tool_input or {},
        id=block_id,
    )


def response(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.message_snapshots = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        self.message_snapshots.append(copy.deepcopy(kwargs.get("messages", [])))
        if not self.responses:
            raise AssertionError("No fake response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def install_common_agent_mocks(monkeypatch):
    monkeypatch.setattr(agent_loop, "consume_cron_queue", lambda: [])
    monkeypatch.setattr(agent_loop, "collect_background_results", lambda: [])
    monkeypatch.setattr(agent_loop, "prepare_context", lambda messages: messages)
    monkeypatch.setattr(agent_loop, "update_context", lambda context, messages: context)
    monkeypatch.setattr(agent_loop, "assemble_system_prompt", lambda context: "system")
    monkeypatch.setattr(agent_loop, "trigger_hooks", lambda *args: None)
    monkeypatch.setattr(agent_loop, "should_run_background", lambda name, tool_input: False)


def test_text_only_response_stops_without_tool_use(monkeypatch):
    install_common_agent_mocks(monkeypatch)
    fake_client = FakeClient([response([text_block("done")])])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "hello"}]
    agent_loop.agent_loop(messages, {})

    assert len(fake_client.messages.calls) == 1
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"][0].text == "done"


def test_read_file_tool_use_executes_and_appends_tool_result(tmp_path, monkeypatch):
    from aqours_code import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    (tmp_path / "hello.txt").write_text("hello Aqours_code")
    fake_client = FakeClient([
        response([tool_block("read_file", {"path": "hello.txt"})]),
        response([text_block("finished")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "read file"}]
    agent_loop.agent_loop(messages, {})

    assert len(fake_client.messages.calls) == 2
    tool_message = messages[-2]
    assert tool_message["role"] == "user"
    assert tool_message["content"][0]["type"] == "tool_result"
    assert tool_message["content"][0]["content"] == "hello Aqours_code"
    assert messages[-1]["content"][0].text == "finished"


def _result_content(messages, tool_use_id: str) -> str:
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tool_use_id
            ):
                return str(block.get("content", ""))
    raise AssertionError(f"missing tool result {tool_use_id}")


def test_result_above_old_8k_limit_reaches_first_provider_request_intact(
    tmp_path,
    monkeypatch,
):
    from aqours_code import basic_tools, compact

    real_prepare_context = agent_loop.prepare_context
    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "prepare_context", real_prepare_context)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    full_result = "HEAD-8K\n" + (
        "x" * (9_000 * compact.ESTIMATED_CHARS_PER_TOKEN)
    ) + "\nTAIL-8K"
    (tmp_path / "medium.txt").write_text(full_result, encoding="utf-8")
    fake_client = FakeClient([
        response([tool_block("read_file", {"path": "medium.txt"}, "medium")]),
        response([text_block("finished")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "read the medium file"}]
    agent_loop.agent_loop(messages, {})

    first_request_with_result = fake_client.messages.message_snapshots[1]
    assert _result_content(first_request_with_result, "medium") == full_result
    assert "[Large tool result omitted]" not in json.dumps(
        first_request_with_result,
        default=str,
    )


def test_oversized_read_file_uses_source_path_and_head_tail_preview(
    tmp_path,
    monkeypatch,
):
    from aqours_code import basic_tools, compact

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    result_dir = tmp_path / ".task_outputs" / "tool-results"
    monkeypatch.setattr(agent_loop, "TOOL_RESULTS_DIR", result_dir)
    full_result = "READ-HEAD\n" + (
        "r" * (
            compact.MAX_INLINE_TOOL_RESULT_TOKENS
            * compact.ESTIMATED_CHARS_PER_TOKEN
            + 20
        )
    ) + "\nREAD-TAIL"
    source = tmp_path / "large.txt"
    source.write_text(full_result, encoding="utf-8")
    recorded = []
    monkeypatch.setattr(
        agent_loop,
        "record_tool_result",
        lambda tool_use_id, tool, result, **metadata: recorded.append({
            "tool_use_id": tool_use_id,
            "tool": tool,
            "result": result,
            **metadata,
        }),
    )
    fake_client = FakeClient([
        response([tool_block("read_file", {"path": "large.txt"}, "large-read")]),
        response([text_block("finished")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "read the large file"}]
    agent_loop.agent_loop(messages, {})

    preview = _result_content(
        fake_client.messages.message_snapshots[1],
        "large-read",
    )
    assert "externalized: true" in preview
    assert "incomplete: true" in preview
    assert 'source_path: "large.txt"' in preview
    assert "requested_offset: 0" in preview
    assert "requested_limit: null" in preview
    assert "total_lines: 3" in preview
    assert "READ-HEAD" in preview
    assert "READ-TAIL" in preview
    assert "--- head preview ---" in preview
    assert "--- tail preview ---" in preview
    assert not result_dir.exists()
    assert recorded[-1]["externalized"] is True
    assert recorded[-1]["backing_path"] == "large.txt"
    assert recorded[-1]["original_estimated_tokens"] > 24_000
    assert len(recorded[-1]["digest"]) == 64


def test_oversized_non_file_output_is_recoverable_from_tool_results_dir(
    tmp_path,
    monkeypatch,
):
    from aqours_code import basic_tools, compact

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    result_dir = tmp_path / ".task_outputs" / "tool-results"
    monkeypatch.setattr(agent_loop, "TOOL_RESULTS_DIR", result_dir)
    full_result = "BASH-HEAD\n" + (
        "b" * (
            compact.MAX_INLINE_TOOL_RESULT_TOKENS
            * compact.ESTIMATED_CHARS_PER_TOKEN
            + 20
        )
    ) + "\nBASH-TAIL"
    class VerboseExecutor:
        def execute(self, command, cwd, timeout):
            return {
                "stdout": full_result,
                "stderr": "",
                "timed_out": False,
                "returncode": 0,
            }

    monkeypatch.setattr(basic_tools, "COMMAND_EXECUTOR", VerboseExecutor())
    recorded = []
    monkeypatch.setattr(
        agent_loop,
        "record_tool_result",
        lambda tool_use_id, tool, result, **metadata: recorded.append({
            "tool_use_id": tool_use_id,
            "tool": tool,
            "result": result,
            **metadata,
        }),
    )
    unsafe_id = "../../unsafe/tool"
    fake_client = FakeClient([
        response([tool_block("bash", {"command": "ignored"}, unsafe_id)]),
        response([text_block("finished")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "run a verbose command"}]
    agent_loop.agent_loop(messages, {})

    preview = _result_content(
        fake_client.messages.message_snapshots[1],
        unsafe_id,
    )
    assert "incomplete: true" in preview
    assert "BASH-HEAD" in preview
    assert "BASH-TAIL" in preview
    assert "--- head preview ---" in preview
    assert "--- tail preview ---" in preview
    output_files = list(result_dir.glob("*.txt"))
    assert len(output_files) == 1
    output_file = output_files[0]
    assert output_file.parent == result_dir
    assert output_file.read_text(encoding="utf-8") == full_result
    relative_path = output_file.relative_to(tmp_path).as_posix()
    assert basic_tools.run_read(
        relative_path,
        offset=0,
        limit=1,
        cwd=tmp_path,
    ) == "BASH-HEAD\n... (2 more lines)"
    assert basic_tools.run_read(
        relative_path,
        offset=2,
        limit=1,
        cwd=tmp_path,
    ) == "BASH-TAIL"
    assert recorded[-1]["externalized"] is True
    assert recorded[-1]["backing_path"] == relative_path
    assert recorded[-1]["original_chars"] == len(full_result)
    assert recorded[-1]["original_lines"] == 3
    assert len(recorded[-1]["digest"]) == 64


def test_glob_double_star_recurses_into_nested_source_tree(tmp_path):
    from aqours_code import basic_tools

    nested = tmp_path / "src" / "inventory_service"
    nested.mkdir(parents=True)
    (nested / "service.py").write_text("class Service: pass\n", encoding="utf-8")
    (tmp_path / "top.py").write_text("TOP = True\n", encoding="utf-8")

    matches = [path.replace("\\", "/") for path in
               basic_tools.run_glob("**/*.py", cwd=tmp_path).splitlines()]

    assert matches == ["src/inventory_service/service.py", "top.py"]


def test_unknown_tool_returns_unknown_tool_result(monkeypatch):
    install_common_agent_mocks(monkeypatch)
    fake_client = FakeClient([
        response([tool_block("missing_tool", {})]),
        response([text_block("finished")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "call unknown"}]
    agent_loop.agent_loop(messages, {})

    tool_results = [
        block
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_results[-1]["content"] == "Unknown: missing_tool"


def test_compact_tool_use_calls_compact_history(monkeypatch):
    install_common_agent_mocks(monkeypatch)
    fake_client = FakeClient([
        response([tool_block("compact", {})]),
        response([text_block("after compact")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)
    compact_calls = []

    def fake_compact_history(messages, **kwargs):
        compact_calls.append(list(messages))
        return [{"role": "user", "content": "compacted"}]

    monkeypatch.setattr(agent_loop, "compact_history", fake_compact_history)

    messages = [{"role": "user", "content": "compact please"}]
    agent_loop.agent_loop(messages, {})

    assert len(compact_calls) == 1
    assert any(msg.get("content") == "[Compacted. Continue with summarized context.]" for msg in messages)
    assert messages[-1]["content"][0].text == "after compact"


def test_compact_tool_preserves_tool_result_pair_when_recent_tail_is_kept(
        monkeypatch):
    install_common_agent_mocks(monkeypatch)
    compact_call = tool_block("compact", {}, "compact_1")
    fake_client = FakeClient([
        response([compact_call]),
        response([text_block("after compact")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)
    monkeypatch.setattr(
        agent_loop, "compact_history",
        lambda messages, **kwargs: [
            {"role": "user", "content": "checkpoint"},
            messages[-1],
        ],
    )

    messages = [{"role": "user", "content": "compact a long history"}]
    agent_loop.agent_loop(messages, {})

    paired_result = messages[-2]
    assert paired_result["role"] == "user"
    assert paired_result["content"][0]["type"] == "tool_result"
    assert paired_result["content"][0]["tool_use_id"] == "compact_1"


def test_context_error_runs_reactive_compact_and_retries_only_once(
        monkeypatch):
    install_common_agent_mocks(monkeypatch)
    fake_client = FakeClient([
        RuntimeError("context_length_exceeded"),
        RuntimeError("context_length_exceeded"),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)
    compact_calls = []

    def fake_reactive(messages, **kwargs):
        compact_calls.append(list(messages))
        return messages

    monkeypatch.setattr(agent_loop, "reactive_compact", fake_reactive)
    messages = [{"role": "user", "content": "retry compact once"}]

    agent_loop.agent_loop(messages, {})

    assert len(fake_client.messages.calls) == 2
    assert len(compact_calls) == 1
    assert "context_length_exceeded" in str(messages[-1]["content"])


def test_tool_use_then_text_completes_full_round(monkeypatch):
    install_common_agent_mocks(monkeypatch)
    fake_client = FakeClient([
        response([tool_block("glob", {"pattern": "*.md"})]),
        response([text_block("all done")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "glob"}]
    agent_loop.agent_loop(messages, {})

    assert len(fake_client.messages.calls) == 2
    assert messages[-2]["content"][0]["type"] == "tool_result"
    assert messages[-1]["content"][0].text == "all done"


def test_multi_step_task_can_edit_without_todo(tmp_path, monkeypatch):
    from aqours_code import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    fake_client = FakeClient([
        response([tool_block("write_file", {
            "path": "a.txt", "content": "written without a checklist",
        })]),
        response([text_block("all done")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{
        "role": "user",
        "content": "Create 3 files, then read them, and summarize the file contents.",
    }]
    agent_loop.agent_loop(messages, {})

    tool_results = [
        block
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_results[0]["content"].startswith("Wrote")
    assert (tmp_path / "a.txt").read_text() == "written without a checklist"
    assert messages[-1]["content"][0].text == "all done"


def test_todo_is_optional_for_code_changes(tmp_path, monkeypatch):
    from aqours_code import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    (tmp_path / "README.md").write_text(
        "Contract: preserve the public API.\n", encoding="utf-8")
    (tmp_path / "service.py").write_text("broken = True\n", encoding="utf-8")
    fake_client = FakeClient([
        response([tool_block("read_file", {"path": "README.md"}, "read_contract")]),
        response([tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "broken", "new_text": "fixed"},
            "edit_without_todo",
        )]),
        response([text_block("done without a checklist")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{
        "role": "user",
        "content": "Fix this README contract bug, preserve the API, and run tests.",
    }]
    agent_loop.agent_loop(messages, {})

    assert (tmp_path / "service.py").read_text(encoding="utf-8") == "fixed = True\n"
    assert len(fake_client.messages.calls) == 3
    assert messages[-1]["content"][0].text == "done without a checklist"


def test_todo_write_uses_a_lightweight_schema_and_stable_ids():
    from aqours_code import basic_tools

    basic_tools.CURRENT_TODOS.clear()
    try:
        output = basic_tools.run_todo_write([
            {"content": "Implement rollback", "status": "in_progress"},
            {"content": "Run rollback tests", "status": "pending"},
        ])

        assert output == "Updated 2 todos"
        assert basic_tools.CURRENT_TODOS == [
            {
                "id": "todo:1",
                "content": "Implement rollback",
                "status": "in_progress",
            },
            {
                "id": "todo:2",
                "content": "Run rollback tests",
                "status": "pending",
            },
        ]

        updated = basic_tools.run_todo_write([
            {
                "id": "todo:1",
                "content": "Implement safe rollback",
                "status": "completed",
            },
            {"id": "todo:2", "status": "in_progress"},
        ])
        assert updated == "Updated 2 todos"
        assert [item["id"] for item in basic_tools.CURRENT_TODOS] == [
            "todo:1", "todo:2",
        ]
        assert [item["status"] for item in basic_tools.CURRENT_TODOS] == [
            "completed", "in_progress",
        ]
        assert basic_tools.CURRENT_TODOS[0]["content"] == (
            "Implement safe rollback"
        )
    finally:
        basic_tools.CURRENT_TODOS.clear()


def test_deprecated_todo_fields_are_discarded():
    from aqours_code import basic_tools

    basic_tools.CURRENT_TODOS.clear()
    try:
        output = basic_tools.run_todo_write([{
            "content": "Run regression tests",
            "status": "completed",
            "kind": "acceptance",
            "evidence": "tests passed",
            "evidence_sources": {"tests": ["pytest"]},
        }])

        assert output == "Updated 1 todos"
        assert basic_tools.CURRENT_TODOS == [{
            "id": "todo:1",
            "content": "Run regression tests",
            "status": "completed",
        }]

        oversized = basic_tools.run_todo_write([
            {"content": f"step {index}", "status": "pending"}
            for index in range(33)
        ])
        assert oversized == "Error: todos may contain at most 32 items"
    finally:
        basic_tools.CURRENT_TODOS.clear()


def test_unfinished_todo_gets_one_final_reminder(monkeypatch):
    from aqours_code import basic_tools

    install_common_agent_mocks(monkeypatch)
    pending = "Run the focused regression test"
    fake_client = FakeClient([
        response([tool_block("todo_write", {"todos": [
            {"content": "Apply fix", "status": "completed"},
            {"content": pending, "status": "pending"},
        ]}, "todo_pending")]),
        response([text_block("everything is complete too early")]),
        response([text_block("still incomplete")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)
    messages = [{"role": "user", "content": "Fix the bug and run tests."}]

    try:
        agent_loop.agent_loop(messages, {})
    finally:
        basic_tools.CURRENT_TODOS.clear()

    assert len(fake_client.messages.calls) == 3
    assert any(
        "<todo_completion_reminder>" in str(message.get("content"))
        and pending in str(message.get("content"))
        for message in fake_client.messages.calls[-1]["messages"]
    )
    final_text = agent_loop.extract_text(messages[-1]["content"])
    assert "still incomplete" in final_text
    assert "Todo checklist incomplete" in final_text
    assert pending in final_text


def test_max_tokens_triggers_continuation_path(monkeypatch):
    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "MODEL_PROVIDER", "openai")
    fake_client = FakeClient([
        response([text_block("partial")], stop_reason="max_tokens"),
        response([text_block("complete")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "long answer"}]
    agent_loop.agent_loop(messages, {})

    assert len(fake_client.messages.calls) == 2
    assert fake_client.messages.calls[1]["max_tokens"] == agent_loop.ESCALATED_MAX_TOKENS
    assert messages[-1]["content"][0].text == "complete"


def test_deepseek_thinking_max_tokens_escalates_to_128000(monkeypatch):
    from aqours_code import recovery

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "MODEL_PROVIDER", "deepseek")
    monkeypatch.setattr(recovery, "PRIMARY_MODEL", "deepseek-v4-flash")
    fake_client = FakeClient([
        response([], stop_reason="max_tokens"),
        response([text_block("complete")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "long answer"}]
    agent_loop.agent_loop(messages, {})

    assert [
        call["max_tokens"] for call in fake_client.messages.calls
    ] == [8000, 128000]
    assert messages[-1]["content"][0].text == "complete"


def test_empty_max_tokens_response_is_not_replayed(monkeypatch):
    from aqours_code import recovery

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "MODEL_PROVIDER", "deepseek")
    monkeypatch.setattr(recovery, "PRIMARY_MODEL", "deepseek-v4-flash")
    first_truncation = response([], stop_reason="max_tokens")
    first_truncation.reasoning_content = "unfinished reasoning"
    second_truncation = response([], stop_reason="max_tokens")
    second_truncation.reasoning_content = "still unfinished reasoning"
    fake_client = FakeClient([
        first_truncation,
        second_truncation,
        response([text_block("complete")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "long answer"}]
    agent_loop.agent_loop(messages, {})

    assert [
        call["max_tokens"] for call in fake_client.messages.calls
    ] == [
        agent_loop.DEFAULT_MAX_TOKENS,
        128000,
    ]
    assert not any(
        message.get("role") == "assistant"
        and not agent_loop.extract_text(message.get("content"))
        and not agent_loop.has_tool_use(message.get("content"))
        for message in messages
    )
    assert not any("reasoning_content" in message for message in messages)
    assert sum(
        message.get("content") == agent_loop.CONTINUATION_PROMPT
        for message in messages
    ) == 0


def test_max_tokens_recovery_retries_can_be_disabled(monkeypatch):
    from aqours_code import recovery

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "MODEL_PROVIDER", "deepseek")
    monkeypatch.setattr(recovery, "PRIMARY_MODEL", "deepseek-v4-flash")
    fake_client = FakeClient([
        response([], stop_reason="max_tokens")
        for _index in range(4)
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "long answer"}]
    agent_loop.agent_loop(messages, {})

    assert [
        call["max_tokens"] for call in fake_client.messages.calls
    ] == [8000, 128000]
    assert sum(
        message.get("content") == agent_loop.CONTINUATION_PROMPT
        for message in messages
    ) == agent_loop.MAX_RECOVERY_RETRIES == 0


def test_run_agent_task_uses_injected_fake_model_client(tmp_path):
    fake_client = FakeClient([response([text_block("task complete")])])
    trace_path = tmp_path / "trace.jsonl"

    result = agent_loop.run_agent_task(
        "say done",
        str(tmp_path),
        str(trace_path),
        model_client=fake_client,
        model_provider="test",
        model="fake",
    )

    assert result["final_answer"] == "task complete"
    assert trace_path.exists()
    assert len(fake_client.messages.calls) == 1


def test_real_context_pipeline_keeps_eight_reads_visible_before_edit(
        tmp_path, monkeypatch):
    from aqours_code import basic_tools

    for index in range(8):
        (tmp_path / f"file_{index}.txt").write_text(
            f"value {index}\n" + chr(97 + index) * 500,
            encoding="utf-8",
        )

    class InspectThenEditClient:
        def __init__(self):
            self.messages = self
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return response([
                    tool_block("read_file", {"path": f"file_{index}.txt"},
                               f"read_{index}")
                    for index in range(8)
                ])
            if self.calls == 2:
                result_message = next(
                    message for message in reversed(kwargs["messages"])
                    if message.get("role") == "user"
                    and isinstance(message.get("content"), list)
                    and any(block.get("type") == "tool_result"
                            for block in message["content"])
                )
                results = [block["content"]
                           for block in result_message["content"]]
                assert len(results) == 8
                assert results[0].startswith("value 0")
                assert results[-1].startswith("value 7")
                return response([tool_block(
                    "edit_file",
                    {"path": "file_0.txt", "old_text": "value 0",
                     "new_text": "fixed 0"},
                    "edit_1",
                )])
            return response([text_block("inspected and edited")])

    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    client = InspectThenEditClient()

    result = agent_loop.run_agent_task(
        "Inspect eight files and correct the first one.",
        str(tmp_path),
        str(tmp_path / "trace.jsonl"),
        model_client=client,
        model_provider="test",
        model="fake",
    )

    assert client.calls == 3
    assert (tmp_path / "file_0.txt").read_text(encoding="utf-8").startswith(
        "fixed 0")
    assert result["final_answer"] == "inspected and edited"


def test_turns_and_tool_calls_remain_trace_metrics_without_loop_limits(tmp_path):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    fake_client = FakeClient([
        response([tool_block("read_file", {"path": "note.txt"}, "read_1")]),
        response([tool_block("read_file", {"path": "note.txt"}, "read_2")]),
        response([text_block("done after two tools")]),
    ])
    trace_path = tmp_path / "trace.jsonl"

    result = agent_loop.run_agent_task(
        "read the note twice",
        str(tmp_path),
        str(trace_path),
        model_client=fake_client,
        model_provider="test",
        model="fake",
    )

    metrics = run_eval.trace_metrics(trace_path)
    assert result["final_answer"] == "done after two tools"
    assert metrics["llm_requests"] == 3
    assert metrics["tool_calls"] == 2
    assert metrics["read_file_calls"] == 2
    assert metrics["tool_counts"]["read_file"] == 2
