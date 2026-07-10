from types import SimpleNamespace

from codepilot_s20 import agent_loop


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

    def create(self, **kwargs):
        self.calls.append(kwargs)
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
    monkeypatch.setattr(agent_loop, "rounds_since_todo", 0)
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
    from codepilot_s20 import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    (tmp_path / "hello.txt").write_text("hello codepilot")
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
    assert tool_message["content"][0]["content"] == "hello codepilot"
    assert messages[-1]["content"][0].text == "finished"


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

    def fake_compact_history(messages):
        compact_calls.append(list(messages))
        return [{"role": "user", "content": "compacted"}]

    monkeypatch.setattr(agent_loop, "compact_history", fake_compact_history)

    messages = [{"role": "user", "content": "compact please"}]
    agent_loop.agent_loop(messages, {})

    assert len(compact_calls) == 1
    assert any(msg.get("content") == "[Compacted. Continue with summarized context.]" for msg in messages)
    assert messages[-1]["content"][0].text == "after compact"


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


def test_multi_step_task_requires_todo_before_tools(tmp_path, monkeypatch):
    from codepilot_s20 import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    fake_client = FakeClient([
        response([tool_block("write_file", {"path": "a.txt", "content": "too early"})]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Create files", "status": "in_progress"},
            {"content": "Read files", "status": "pending"},
            {"content": "Summarize", "status": "pending"},
        ]})]),
        response([tool_block("write_file", {"path": "a.txt", "content": "after todo"})]),
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
    assert tool_results[0]["content"].startswith("Tool not run: this multi-step task")
    assert (tmp_path / "a.txt").read_text() == "after todo"
    assert messages[-1]["content"][0].text == "all done"


def test_max_tokens_triggers_continuation_path(monkeypatch):
    install_common_agent_mocks(monkeypatch)
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
