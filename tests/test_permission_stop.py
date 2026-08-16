from types import SimpleNamespace

from aqours_code import agent_loop
from aqours_code import hooks
from aqours_code import trace
from aqours_code.command_executor import LocalCommandExecutor


class DeniedThenTextClient:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(type="text", text="I will delete everything."),
                    SimpleNamespace(
                        type="tool_use",
                        id="call_1",
                        name="bash",
                        input={"command": "Remove-Item -Recurse -Force ."},
                    )
                ],
            )
        if self.calls == 2:
            blocked = kwargs["messages"][-1]["content"][0]
            assert blocked["tool_use_id"] == "call_1"
            assert blocked["content"].startswith("Tool not run:")
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[SimpleNamespace(
                    type="tool_use",
                    id="call_2",
                    name="bash",
                    input={"command": "echo safe"},
                )],
            )
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="recovered safely")],
        )


def test_agent_loop_recovers_after_ordinary_policy_block(tmp_path):
    fake_client = DeniedThenTextClient()
    executor = LocalCommandExecutor()

    result = agent_loop.run_agent_task(
        "inspect safely",
        str(tmp_path),
        model_client=fake_client,
        model_provider="test",
        model="fake",
        command_executor=executor,
    )

    assert fake_client.calls == 3
    assert executor.command_execution_count == 1
    assert result["final_answer"] == "recovered safely"


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


def test_noninteractive_permission_denial_stops_safely_without_eof(tmp_path):
    class DestructiveClient:
        def __init__(self, block):
            self.messages = self
            self.calls = 0
            self.block = block

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("permission denial should stop the loop")
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[self.block],
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

        assert client.calls == 1
        assert result["final_answer"].startswith("Permission denied:")
        assert "interactive approval" in result["final_answer"]


def test_interactive_permission_approval_still_uses_input(monkeypatch):
    prompts = []
    monkeypatch.setattr(hooks, "APPROVAL_MODE", "interactive")
    monkeypatch.setattr(
        "builtins.input", lambda prompt: prompts.append(prompt) or "yes")

    result = hooks.permission_hook(approval_block(
        "bash", {"command": "echo approved > /etc/aqours-code-eval"}))

    assert result is None
    assert prompts == ["  Allow? [y/N] "]


def test_user_cancel_still_terminates_agent_loop(monkeypatch, tmp_path):
    class ApprovalClient:
        def __init__(self):
            self.messages = self
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("user cancellation must stop the loop")
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[approval_block(
                    "bash", {"command": "echo unsafe > /etc/aqours-code-eval"}
                )],
            )

    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    client = ApprovalClient()
    executor = LocalCommandExecutor()

    result = agent_loop.run_agent_task(
        "request an approval-gated operation",
        str(tmp_path),
        model_client=client,
        model_provider="test",
        model="fake",
        command_executor=executor,
        approval_mode="interactive",
    )

    assert client.calls == 1
    assert executor.command_execution_count == 0
    assert result["final_answer"] == "Permission denied by user"
