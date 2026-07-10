from types import SimpleNamespace

from codepilot_s20 import agent_loop
from codepilot_s20 import trace


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
