from types import SimpleNamespace

from codepilot_s20 import basic_tools, hooks, message_bus, protocol


def _bash_block(command: str):
    return SimpleNamespace(name="bash", input={"command": command})


def test_message_bus_recreates_missing_mailbox_dir():
    message_bus.MAILBOX_DIR.rmdir()
    message_bus.BUS.send("lead", "self", "hello")
    assert (message_bus.MAILBOX_DIR / "self.jsonl").exists()


def test_request_shutdown_survives_missing_mailbox_dir():
    message_bus.MAILBOX_DIR.rmdir()
    result = protocol.run_request_shutdown("self")
    assert result == "Shutdown request sent to self"
    assert (message_bus.MAILBOX_DIR / "self.jsonl").exists()


def test_permission_hook_blocks_delete_command_variants():
    commands = [
        'rmdir /s /q "."',
        "Remove-Item -Recurse -Force .",
        "del /s /q *",
        "rd /s /q %cd%",
        "rm -rf .",
    ]
    for command in commands:
        assert hooks.permission_hook(_bash_block(command)) == "Permission denied: delete commands are disabled for bash"


def test_run_bash_blocks_delete_command_even_without_hook(tmp_path):
    output = basic_tools.run_bash("Remove-Item -Recurse -Force .", cwd=tmp_path)
    assert output == "Permission denied: delete commands are disabled for bash"
    assert tmp_path.exists()
