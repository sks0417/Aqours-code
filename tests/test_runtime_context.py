from codepilot_s20 import prompts
from codepilot_s20.runtime_context import (
    detect_runtime_context,
    format_runtime_context_for_prompt,
)


def test_detect_runtime_context_includes_command_guidance(tmp_path):
    data = detect_runtime_context(tmp_path)

    assert data["os"]
    assert data["workdir"] == str(tmp_path.resolve())
    assert isinstance(data["command_hints"], list)
    assert data["command_hints"]


def test_runtime_context_is_in_system_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "WORKDIR", tmp_path)

    system_prompt = prompts.assemble_system_prompt({})

    assert "Runtime environment:" in system_prompt
    assert f"- Working directory: {tmp_path.resolve()}" in system_prompt
    assert "Command guidance:" in system_prompt


def test_runtime_context_prompt_format_is_readable(tmp_path):
    text = format_runtime_context_for_prompt(detect_runtime_context(tmp_path))

    assert "- OS:" in text
    assert "- Shell:" in text
    assert "- Path separator:" in text
