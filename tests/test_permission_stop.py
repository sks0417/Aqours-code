import copy
import json
from types import SimpleNamespace

from aqours_code import agent_loop
from aqours_code import hooks
from aqours_code import trace
from aqours_code.command_executor import LocalCommandExecutor


class DeniedThenSafeClient:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append({
            **kwargs,
            "messages": copy.deepcopy(kwargs["messages"]),
        })
        call_number = len(self.calls)
        if call_number == 1:
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id="edit_service",
                        name="edit_file",
                        input={
                            "path": "service.py",
                            "old_text": "VALUE = 1",
                            "new_text": "VALUE = 2",
                        },
                    )
                ],
            )
        if call_number == 2:
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[SimpleNamespace(
                    type="tool_use",
                    id="call_delete",
                    name="bash",
                    input={"command": (
                        "echo tests passed; rm -f /tmp/pytest_out.txt"
                    )},
                )],
            )
        if call_number == 3:
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[SimpleNamespace(
                    type="tool_use",
                    id="call_safe",
                    name="bash",
                    input={"command": "echo safe verification"},
                )],
            )
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="completed safely")],
        )


def test_blocked_delete_returns_to_leader_then_safe_command_and_final(
    monkeypatch, tmp_path,
):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    fake_client = DeniedThenSafeClient()
    executed_bash = []
    original_call_tool_handler = agent_loop.call_tool_handler

    def recording_call_tool_handler(
        handler, tool_input, tool_name, *, tool_use_id="",
    ):
        if tool_name == "bash":
            executed_bash.append(tool_input["command"])
        return original_call_tool_handler(
            handler, tool_input, tool_name, tool_use_id=tool_use_id,
        )

    monkeypatch.setattr(
        agent_loop, "call_tool_handler", recording_call_tool_handler,
    )
    trace_path = tmp_path / "trace.jsonl"

    result = agent_loop.run_agent_task(
        "Implement atomic rollback and idempotency, then run regression tests.",
        str(tmp_path),
        trace_path=str(trace_path),
        model_client=fake_client,
        model_provider="scripted",
        model="scripted",
        command_executor=LocalCommandExecutor(),
        approval_mode="non_interactive",
    )

    assert result["final_answer"] == "completed safely"
    assert len(fake_client.calls) == 4
    assert executed_bash == ["echo safe verification"]
    assert (tmp_path / "service.py").read_text(
        encoding="utf-8") == "VALUE = 2\n"
    next_request = fake_client.calls[2]["messages"]
    denied_call = next(
        block
        for message in next_request if message["role"] == "assistant"
        for block in message["content"]
        if getattr(block, "id", "") == "call_delete"
    )
    denied_result = next(
        item
        for message in next_request if message["role"] == "user"
        and isinstance(message["content"], list)
        for item in message["content"] if isinstance(item, dict)
        and item.get("tool_use_id") == "call_delete"
    )
    assert denied_call.input["command"].endswith(
        "rm -f /tmp/pytest_out.txt")
    assert denied_result["tool_use_id"] == denied_call.id
    assert "Permission denied" in denied_result["content"]

    events = [
        json.loads(line) for line in trace_path.read_text(
            encoding="utf-8").splitlines()
    ]
    blocked = next(
        event for event in events
        if event.get("type") == "hook"
        and event.get("name") == "PreToolUse"
        and event.get("decision") == "blocked"
        and event.get("tool_use_id") == "call_delete"
    )
    traced_result = next(
        event for event in events
        if event.get("type") == "tool_result"
        and event.get("tool_use_id") == "call_delete"
    )
    assert blocked["recoverable"] is False
    assert "Permission denied" in blocked["reason"]
    assert "Permission denied" in traced_result["content"]


class RecoverableCleanupClient:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(type="text", text="I will inspect files."),
                    SimpleNamespace(
                        type="tool_use",
                        id="call_temp_cleanup",
                        name="bash",
                        input={"command": (
                            "dir stress_files\\*.txt /b > temp_list.txt "
                            "& find /c /v \"\" < temp_list.txt "
                            "& del temp_list.txt"
                        )},
                    )
                ],
            )
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Recovered with a read-only approach.")],
        )


def test_recoverable_temp_cleanup_rejection_continues(monkeypatch, tmp_path):
    fake_client = RecoverableCleanupClient()
    monkeypatch.setattr(agent_loop, "client", fake_client)

    run = trace.start_run(
        "count files",
        workdir=tmp_path,
        model_provider="test",
        model="test",
    )
    messages = [{"role": "user", "content": "count files without deleting"}]
    agent_loop.agent_loop(messages, {})

    metadata = trace.get_run_summary(run.run_id, workdir=tmp_path)
    assert fake_client.calls == 2
    assert metadata["status"] == "success"
    assert metadata["blocked_count"] == 0
    assert "Recovered with a read-only approach." in (run.final_path.read_text(encoding="utf-8"))


def approval_block(name: str, data: dict):
    return SimpleNamespace(type="tool_use", id="approval", name=name, input=data)


def test_noninteractive_destructive_bash_denies_without_input(monkeypatch):
    monkeypatch.setattr(hooks, "APPROVAL_MODE", "non_interactive")
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("input must not be called")),
    )

    result = hooks.permission_hook(approval_block(
        "bash", {"command": "echo unsafe > /etc/aqours-code-eval"}))

    assert result["kind"] == "tool_policy_rejection"
    assert result["recoverable"] is False
    assert result["message"].startswith("Permission denied:")
    assert "interactive approval" in result["message"]


def test_noninteractive_deploy_mcp_denies_without_input(monkeypatch):
    monkeypatch.setattr(hooks, "APPROVAL_MODE", "non_interactive")
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("input must not be called")),
    )

    result = hooks.permission_hook(approval_block(
        "mcp__deploy__trigger", {"environment": "production"}))

    assert result["kind"] == "tool_policy_rejection"
    assert result["recoverable"] is False
    assert "mcp__deploy__trigger" in result["message"]


def test_noninteractive_permission_denial_returns_to_model_without_eof(tmp_path):
    class DestructiveClient:
        def __init__(self, block):
            self.messages = self
            self.calls = 0
            self.block = block

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    stop_reason="tool_use",
                    content=[self.block],
                )
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(
                    type="text", text="continued safely after denial")],
            )

    blocks = [
        approval_block(
            "bash", {"command": "echo unsafe > /etc/aqours-code-eval"}),
        approval_block(
            "mcp__deploy__trigger", {"environment": "production"}),
    ]
    for index, block in enumerate(blocks):
        client = DestructiveClient(block)
        result = agent_loop.run_agent_task(
            "attempt an approval-gated operation",
            str(tmp_path / str(index)),
            model_client=client,
            model_provider="scripted",
            model="scripted",
            command_executor=LocalCommandExecutor(),
            approval_mode="non_interactive",
        )

        assert client.calls == 2
        assert result["final_answer"] == "continued safely after denial"


def test_provider_failure_still_terminates_without_another_model_call(tmp_path):
    class FatalProviderClient:
        def __init__(self):
            self.messages = self
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("provider unavailable")

    client = FatalProviderClient()
    result = agent_loop.run_agent_task(
        "Inspect the repository.",
        str(tmp_path),
        model_client=client,
        model_provider="scripted",
        model="scripted",
        command_executor=LocalCommandExecutor(),
    )

    assert client.calls == 1
    assert result["final_answer"] == (
        "[Error] RuntimeError: provider unavailable")


def test_interactive_permission_approval_still_uses_input(monkeypatch):
    prompts = []
    monkeypatch.setattr(hooks, "APPROVAL_MODE", "interactive")
    monkeypatch.setattr(
        "builtins.input", lambda prompt: prompts.append(prompt) or "yes")

    result = hooks.permission_hook(approval_block(
        "bash", {"command": "echo approved > /etc/aqours-code-eval"}))

    assert result is None
    assert prompts == ["  Allow? [y/N] "]
