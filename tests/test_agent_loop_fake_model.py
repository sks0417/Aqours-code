from types import SimpleNamespace

from codepilot_s20 import agent_loop
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


def test_glob_double_star_recurses_into_nested_source_tree(tmp_path):
    from codepilot_s20 import basic_tools

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

    def fake_compact_history(messages):
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
        lambda messages: [{"role": "user", "content": "checkpoint"},
                          messages[-1]],
    )

    messages = [{"role": "user", "content": "compact a long history"}]
    agent_loop.agent_loop(messages, {})

    paired_result = messages[-2]
    assert paired_result["role"] == "user"
    assert paired_result["content"][0]["type"] == "tool_result"
    assert paired_result["content"][0]["tool_use_id"] == "compact_1"


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
    assert tool_results[0]["content"].startswith(
        "Tool not run: before changing files")
    assert (tmp_path / "a.txt").read_text() == "after todo"
    assert messages[-1]["content"][0].text == "all done"


def test_complex_code_task_allows_contract_read_before_todo(tmp_path, monkeypatch):
    from codepilot_s20 import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    (tmp_path / "README.md").write_text(
        "Contract: preserve the public API.\n", encoding="utf-8")
    (tmp_path / "service.py").write_text("broken = True\n", encoding="utf-8")
    fake_client = FakeClient([
        response([tool_block("read_file", {"path": "README.md"}, "read_contract")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix service", "status": "in_progress", "kind": "plan"},
            {"content": "Preserve the public API", "status": "completed",
             "kind": "acceptance", "evidence": "README contract inspected"},
        ]}, "todo_after_read")]),
        response([tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "broken", "new_text": "fixed"},
            "edit_after_contract",
        )]),
        response([text_block("done before final audit")]),
        response([tool_block("read_file", {"path": "README.md"}, "audit_read")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix service", "status": "completed", "kind": "plan"},
            {"content": "Preserve the public API", "status": "completed",
             "kind": "acceptance", "evidence": "README and final diff audited"},
        ]}, "todo_after_audit")]),
        response([text_block("done after final audit")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{
        "role": "user",
        "content": "Fix this README contract bug, preserve the API, and run tests.",
    }]
    try:
        agent_loop.agent_loop(messages, {})
    finally:
        basic_tools.CURRENT_TODOS.clear()

    read_result = messages[2]["content"][0]["content"]
    assert read_result == "Contract: preserve the public API."
    assert "Tool not run" not in read_result
    assert (tmp_path / "service.py").read_text(encoding="utf-8") == "fixed = True\n"
    audit_messages = fake_client.messages.calls[4]["messages"]
    assert any(
        "<acceptance_review>" in str(message.get("content"))
        and "potentially incomplete" in str(message.get("content"))
        for message in audit_messages
    )
    assert messages[-1]["content"][0].text == "done after final audit"


def test_completed_acceptance_todo_requires_evidence():
    from codepilot_s20 import basic_tools

    basic_tools.CURRENT_TODOS.clear()
    try:
        output = basic_tools.run_todo_write([{
            "content": "UnknownSku leaves inventory unchanged",
            "status": "completed",
            "kind": "acceptance",
        }])

        assert output.startswith("Error:")
        assert "requires evidence" in output
        assert basic_tools.CURRENT_TODOS == []

        output = basic_tools.run_todo_write([{
            "content": "UnknownSku leaves inventory unchanged",
            "status": "completed",
            "kind": "acceptance",
            "evidence": "rollback test passes",
        }])
        assert output == "Updated 1 todos (1 acceptance, 0 unverified)"
        assert basic_tools.CURRENT_TODOS[0]["id"] == "accept:1"

        oversized = basic_tools.run_todo_write([
            {"content": f"contract {index}", "status": "pending",
             "kind": "acceptance"}
            for index in range(13)
        ])
        assert oversized == "Error: todos may contain at most 12 acceptance items"
    finally:
        basic_tools.CURRENT_TODOS.clear()


def test_complex_code_task_requires_acceptance_before_edit_and_reviews_it(
    tmp_path, monkeypatch,
):
    from codepilot_s20 import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    (tmp_path / "service.py").write_text("broken = True\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "Contract: UnknownSku leaves inventory unchanged.\n", encoding="utf-8")
    contract = "UnknownSku leaves inventory unchanged"
    fake_client = FakeClient([
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix service", "status": "in_progress"},
        ]}, "todo_plan_only")]),
        response([tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "broken", "new_text": "fixed"},
            "edit_too_early",
        )]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix service", "status": "in_progress", "kind": "plan"},
            {"content": contract, "status": "pending", "kind": "acceptance"},
        ]}, "todo_with_contract")]),
        response([tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "broken", "new_text": "fixed"},
            "edit_after_contract",
        )]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix service", "status": "completed", "kind": "plan"},
            {"content": contract, "status": "pending", "kind": "acceptance"},
        ]}, "todo_ready_for_review")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix service", "status": "completed", "kind": "plan"},
            {"content": contract, "status": "completed", "kind": "acceptance",
             "evidence": "service.py diff reviewed and rollback test passes"},
        ]}, "todo_verified")]),
        response([text_block("verified before contract audit")]),
        response([tool_block("read_file", {"path": "README.md"}, "audit_read")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix service", "status": "completed", "kind": "plan"},
            {"content": contract, "status": "completed", "kind": "acceptance",
             "evidence": "README, final diff, and rollback test audited"},
        ]}, "todo_audited")]),
        response([text_block("verified and complete")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{
        "role": "user",
        "content": ("Fix the code according to the README contract, preserve the "
                    "public API, and run tests."),
    }]
    try:
        agent_loop.agent_loop(messages, {})
        final_todos = [dict(todo) for todo in basic_tools.CURRENT_TODOS]
    finally:
        basic_tools.CURRENT_TODOS.clear()

    tool_results = [
        block["content"]
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert "Acceptance checklist required" in tool_results[0]
    assert tool_results[1].startswith("Tool not run: before changing files")
    assert (tmp_path / "service.py").read_text(encoding="utf-8") == "fixed = True\n"
    review_messages = fake_client.messages.calls[7]["messages"]
    assert any(
        "<acceptance_review>" in str(message.get("content"))
        and contract in str(message.get("content"))
        for message in review_messages
    )
    assert final_todos[-1]["evidence"].startswith("README, final diff")
    assert messages[-1]["content"][0].text == "verified and complete"


def test_acceptance_items_cannot_be_silently_removed_or_rewritten(
    tmp_path, monkeypatch,
):
    from codepilot_s20 import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    (tmp_path / "fingerprint.py").write_text("old = True\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "Contract: fingerprint includes sku and quantity.\n", encoding="utf-8")
    contract = "Fingerprint includes sku and quantity"
    fake_client = FakeClient([
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix fingerprint", "status": "in_progress", "kind": "plan"},
            {"content": contract, "status": "pending", "kind": "acceptance"},
        ]}, "todo_contract")]),
        response([tool_block("edit_file", {
            "path": "fingerprint.py", "old_text": "old", "new_text": "new",
        }, "edit_contract")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix fingerprint", "status": "completed", "kind": "plan"},
            {"content": "Ensure sku and quantity are fingerprinted",
             "status": "pending", "kind": "acceptance"},
        ]}, "todo_reworded_contract")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix fingerprint", "status": "completed", "kind": "plan"},
        ]}, "todo_removed_contract")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix fingerprint", "status": "completed", "kind": "plan"},
            {"content": contract, "status": "completed", "kind": "acceptance",
             "evidence": "fingerprint regression passes"},
        ]}, "todo_restored_contract")]),
        response([text_block("done before audit")]),
        response([tool_block("read_file", {"path": "README.md"}, "audit_read")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Fix fingerprint", "status": "completed", "kind": "plan"},
            {"content": contract, "status": "completed", "kind": "acceptance",
             "evidence": "README, diff, and regression test audited"},
        ]}, "todo_after_audit")]),
        response([text_block("done")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)
    messages = [{
        "role": "user",
        "content": "Fix this README contract bug, preserve behavior, and run tests.",
    }]
    try:
        agent_loop.agent_loop(messages, {})
    finally:
        basic_tools.CURRENT_TODOS.clear()

    results = [
        block["content"]
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert any(
        "Protected acceptance criteria preserved" in result
        and "kept wording" in result
        and contract in result
        for result in results
    )
    assert any(
        "Protected acceptance criteria preserved" in result
        and "restored omitted item" in result
        and contract in result
        for result in results
    )
    assert (tmp_path / "fingerprint.py").read_text(encoding="utf-8") == "new = True\n"
    assert messages[-1]["content"][0].text == "done"


def test_final_contract_audit_can_add_requirement_omitted_from_completed_list(
    tmp_path, monkeypatch,
):
    from codepilot_s20 import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    (tmp_path / "README.md").write_text(
        "Contract A: retries are stable.\n"
        "Contract B: different payloads conflict.\n",
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text(
        "retry_fixed = False\nconflict_fixed = False\n", encoding="utf-8")
    contract_a = "Retries are stable"
    contract_b = "Different payloads conflict"
    fake_client = FakeClient([
        response([tool_block("read_file", {"path": "README.md"}, "initial_read")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Implement consistency fixes", "status": "in_progress",
             "kind": "plan"},
            {"content": contract_a, "status": "pending", "kind": "acceptance"},
        ]}, "initial_todo")]),
        response([tool_block("edit_file", {
            "path": "service.py", "old_text": "retry_fixed = False",
            "new_text": "retry_fixed = True",
        }, "first_edit")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Implement consistency fixes", "status": "completed",
             "kind": "plan"},
            {"content": contract_a, "status": "completed", "kind": "acceptance",
             "evidence": "retry diff reviewed"},
        ]}, "premature_complete")]),
        response([text_block("complete before fresh audit")]),
        response([tool_block("read_file", {"path": "README.md"}, "audit_read")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Implement omitted conflict behavior", "status": "in_progress",
             "kind": "plan"},
            {"content": contract_a, "status": "completed", "kind": "acceptance",
             "evidence": "retry diff reviewed"},
            {"content": contract_b, "status": "pending", "kind": "acceptance"},
        ]}, "audit_adds_missing")]),
        response([tool_block("edit_file", {
            "path": "service.py", "old_text": "conflict_fixed = False",
            "new_text": "conflict_fixed = True",
        }, "missing_fix")]),
        response([tool_block("todo_write", {"todos": [
            {"content": "Implement omitted conflict behavior", "status": "completed",
             "kind": "plan"},
            {"content": contract_a, "status": "completed", "kind": "acceptance",
             "evidence": "retry diff audited"},
            {"content": contract_b, "status": "completed", "kind": "acceptance",
             "evidence": "README and conflict diff audited"},
        ]}, "audit_complete")]),
        response([text_block("complete after fresh audit")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)
    messages = [{
        "role": "user",
        "content": "Fix this README consistency bug, preserve behavior, and run tests.",
    }]
    try:
        agent_loop.agent_loop(messages, {})
        final_todos = [dict(todo) for todo in basic_tools.CURRENT_TODOS]
    finally:
        basic_tools.CURRENT_TODOS.clear()

    review_messages = fake_client.messages.calls[5]["messages"]
    assert any(
        "<acceptance_review>" in str(message.get("content"))
        and "potentially incomplete" in str(message.get("content"))
        for message in review_messages
    )
    assert any(todo["content"] == contract_b for todo in final_todos)
    assert all(todo["status"] == "completed" for todo in final_todos)
    assert "conflict_fixed = True" in (
        tmp_path / "service.py").read_text(encoding="utf-8")
    assert messages[-1]["content"][0].text == "complete after fresh audit"


def test_final_contract_audit_scopes_and_deduplicates_reads(
    tmp_path, monkeypatch,
):
    from codepilot_s20 import basic_tools

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "WORKDIR", tmp_path)
    monkeypatch.setattr(basic_tools, "WORKDIR", tmp_path)
    for path in ("README.md", "service.py", "extra_a.py", "extra_b.py",
                 "extra_c.py"):
        (tmp_path / path).write_text(f"content for {path}\n", encoding="utf-8")
    contract = "Reservation retries preserve state"
    completed = [
        {"content": "Implement reservation fix", "status": "completed",
         "kind": "plan"},
        {"content": contract, "status": "completed", "kind": "acceptance",
         "evidence": "service diff and tests reviewed"},
    ]
    fake_client = FakeClient([
        response([tool_block("todo_write", {"todos": [
            {"content": "Implement reservation fix", "status": "in_progress",
             "kind": "plan"},
            {"content": contract, "status": "pending", "kind": "acceptance"},
        ]}, "initial_todo")]),
        response([tool_block("edit_file", {
            "path": "service.py", "old_text": "content for service.py",
            "new_text": "fixed service",
        }, "service_edit")]),
        response([tool_block("todo_write", {"todos": completed}, "complete")]),
        response([text_block("ready for final")]),
        response([
            tool_block("read_file", {"path": "README.md"}, "audit_readme"),
            tool_block("read_file", {"path": "./README.md"}, "audit_duplicate"),
            tool_block("glob", {"pattern": "**/*.py"}, "audit_glob"),
            tool_block("read_file", {"path": "service.py"}, "audit_changed"),
            tool_block("read_file", {"path": "extra_a.py"}, "audit_third"),
            tool_block("read_file", {"path": "extra_b.py"}, "audit_fourth"),
            tool_block("read_file", {"path": "extra_c.py"}, "audit_over_budget"),
            tool_block("todo_write", {"todos": completed}, "audit_todo"),
        ]),
        response([text_block("done after scoped audit")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)
    messages = [{
        "role": "user",
        "content": "Fix the README reservation contract, preserve behavior, and run tests.",
    }]
    try:
        agent_loop.agent_loop(messages, {})
    finally:
        basic_tools.CURRENT_TODOS.clear()

    audit_prompt = next(
        str(message["content"])
        for message in messages
        if message.get("role") == "user"
        and "<acceptance_review>" in str(message.get("content"))
    )
    assert "service.py" in audit_prompt
    assert "at most 4 read_file calls" in audit_prompt
    assert "inspect its producer function" in audit_prompt
    assert "checking only the caller or comparison site is not evidence" in audit_prompt
    by_id = {
        block["tool_use_id"]: block["content"]
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    assert by_id["audit_readme"].startswith("content for README.md")
    assert "already read" in by_id["audit_duplicate"]
    assert "glob scans are disabled" in by_id["audit_glob"]
    assert by_id["audit_changed"].startswith("fixed service")
    assert by_id["audit_third"].startswith("content for extra_a.py")
    assert by_id["audit_fourth"].startswith("content for extra_b.py")
    assert "read budget reached" in by_id["audit_over_budget"]
    assert messages[-1]["content"][0].text == "done after scoped audit"


def test_ignored_acceptance_review_marks_final_incomplete(monkeypatch):
    from codepilot_s20 import basic_tools

    install_common_agent_mocks(monkeypatch)
    contract = "All documented errors preserve repository state"
    fake_client = FakeClient([
        response([tool_block("todo_write", {"todos": [
            {"content": "Apply fix", "status": "completed", "kind": "plan"},
            {"content": contract, "status": "pending", "kind": "acceptance"},
        ]}, "todo_unverified")]),
        response([text_block("everything is complete before audit")]),
        response([text_block("still complete without recording audit")]),
        response([text_block("everything is complete")]),
    ])
    monkeypatch.setattr(agent_loop, "client", fake_client)
    messages = [{
        "role": "user",
        "content": "Fix the README contract bug, preserve the API, and run tests.",
    }]
    try:
        agent_loop.agent_loop(messages, {})
    finally:
        basic_tools.CURRENT_TODOS.clear()

    assert len(fake_client.messages.calls) == 4
    final_text = agent_loop.extract_text(messages[-1]["content"])
    assert "everything is complete" in final_text
    assert "Acceptance review incomplete" in final_text
    assert "final contract audit was not recorded" in final_text
    assert contract in final_text


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


def test_real_context_pipeline_keeps_eight_reads_visible_before_edit(
        tmp_path, monkeypatch):
    from codepilot_s20 import basic_tools

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

    monkeypatch.setattr(agent_loop, "rounds_since_todo", 0)
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
