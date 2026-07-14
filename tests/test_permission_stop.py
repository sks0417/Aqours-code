from types import SimpleNamespace

from codepilot_s20 import agent_loop
from codepilot_s20 import hooks
from codepilot_s20 import trace
from codepilot_s20.command_executor import LocalCommandExecutor


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
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="try deleting manually")],
        )


def test_agent_loop_stops_after_permission_denied(monkeypatch):
    fake_client = DeniedThenTextClient()
    monkeypatch.setattr(agent_loop, "client", fake_client)

    messages = [{"role": "user", "content": "delete current project"}]
    agent_loop.agent_loop(messages, {})

    assert fake_client.calls == 1
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"][0]["text"] == "Permission denied: delete commands are disabled for bash"


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
        "bash", {"command": "echo unsafe > /etc/codepilot-eval"}))

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
            "bash", {"command": "echo unsafe > /etc/codepilot-eval"}),
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
        "bash", {"command": "echo approved > /etc/codepilot-eval"}))

    assert result is None
    assert prompts == ["  Allow? [y/N] "]
