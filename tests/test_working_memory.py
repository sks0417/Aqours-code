from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from codepilot_s20 import basic_tools, compact, context, prompts, tool_handlers
from codepilot_s20.command_executor import LocalCommandExecutor
from codepilot_s20.runtime import AgentRuntime


def make_runtime(tmp_path: Path, executor=None) -> AgentRuntime:
    return AgentRuntime.create(
        workdir=tmp_path,
        state_root=tmp_path / "state",
        model_client=SimpleNamespace(messages=object()),
        command_executor=executor or LocalCommandExecutor(),
        model_provider="test",
        model="test",
        root_task="working-memory test",
    )


def test_read_file_records_digest_version_and_python_symbols(tmp_path):
    source = "class Service:\n    def reserve(self):\n        return 1\n"
    (tmp_path / "service.py").write_text(source, encoding="utf-8")
    runtime = make_runtime(tmp_path)

    assert "class Service" in basic_tools.run_read(
        "service.py", runtime=runtime,
    )
    basic_tools.run_read("service.py", limit=1, runtime=runtime)

    record = runtime.state.knowledge.files["service.py"]
    assert record.digest == hashlib.sha256(
        (tmp_path / "service.py").read_bytes(),
    ).hexdigest()
    assert record.version == 1
    assert record.read_count == 2
    assert record.evidence_valid is True
    assert runtime.state.knowledge.confirmed_symbols[
        "service.py:Service.reserve"
    ].evidence_valid is True


def test_file_mutation_invalidates_only_evidence_linked_to_that_file(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    runtime = make_runtime(tmp_path)
    basic_tools.run_read("a.py", runtime=runtime)
    basic_tools.run_read("b.py", runtime=runtime)
    knowledge = runtime.state.knowledge
    knowledge.record_contract("contract:a", "alpha remains stable", ["a.py"])
    knowledge.record_reviewer_findings([{
        "file": "b.py",
        "symbol": "beta",
        "requirement": "beta must stay correct",
    }], revision=1)
    basic_tools.run_todo_write([{
        "id": "accept:1",
        "content": "alpha behavior is preserved",
        "status": "completed",
        "kind": "acceptance",
        "evidence": "a.py inspected",
    }], runtime=runtime)

    assert basic_tools.run_edit(
        "a.py", "return 1", "return 3", runtime=runtime,
    ) == "Edited a.py"

    assert knowledge.files["a.py"].version == 2
    assert knowledge.files["a.py"].evidence_valid is False
    assert knowledge.files["b.py"].evidence_valid is True
    assert knowledge.confirmed_contracts["contract:a"].evidence_valid is False
    assert knowledge.acceptance["accept:1"]["evidence_valid"] is False
    assert knowledge.reviewer_findings[
        "review:r1:f1"
    ].evidence_valid is True
    assert knowledge.confirmed_symbols["a.py:alpha"].evidence_valid is False
    assert knowledge.confirmed_symbols["b.py:beta"].evidence_valid is True

    basic_tools.run_read("a.py", runtime=runtime)
    assert knowledge.files["a.py"].evidence_valid is True
    # Reading the new version does not silently revive evidence from v1.
    assert knowledge.confirmed_contracts["contract:a"].evidence_valid is False


def test_test_results_bind_to_the_modified_file_versions(tmp_path):
    class TestExecutor:
        def execute(self, command, cwd, timeout):
            return {
                "command": command,
                "exit_code": 0,
                "stdout": "12 passed",
                "stderr": "",
                "timed_out": False,
                "duration_ms": 5,
            }

    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("other = 1\n", encoding="utf-8")
    runtime = make_runtime(tmp_path, TestExecutor())
    basic_tools.run_read("a.py", runtime=runtime)
    basic_tools.run_write("a.py", "value = 2\n", runtime=runtime)
    basic_tools.run_read("a.py", runtime=runtime)

    assert basic_tools.run_bash(
        "python -m pytest -q", runtime=runtime,
    ) == "12 passed"
    test = runtime.state.knowledge.recent_tests[-1]
    assert test.passed is True
    assert test.validated_versions == {"a.py": 2}

    basic_tools.run_write("b.py", "other = 2\n", runtime=runtime)
    assert test.stale_paths == []
    basic_tools.run_write("a.py", "value = 3\n", runtime=runtime)
    assert test.stale_paths == ["a.py"]


def test_context_and_compact_retain_run_knowledge(tmp_path, monkeypatch):
    (tmp_path / "service.py").write_text(
        "def reserve():\n    return True\n", encoding="utf-8",
    )
    runtime = make_runtime(tmp_path)
    basic_tools.run_read("service.py", runtime=runtime)
    live = context.update_context({}, [], runtime)

    assert live["working_memory"]["read_files"]["service.py"]["version"] == 1
    system = prompts.assemble_system_prompt(live, runtime)
    assert "RunKnowledge" in system
    assert "service.py v1 valid" in system
    assert "function reserve" in system

    monkeypatch.setattr(compact, "record_event", lambda *args, **kwargs: None)
    messages = [
        {"role": "user", "content": "inspect service"},
        {"role": "assistant", "content": "old result"},
    ]
    result = compact.compact_history(
        messages,
        allow_model_summary=False,
        runtime=runtime,
    )
    assert runtime.state.knowledge.files["service.py"].evidence_valid is True
    assert "RunKnowledge retained outside raw message history" in json.dumps(
        result,
    )


def test_successful_worktree_integration_invalidates_changed_paths_only(
    tmp_path, monkeypatch,
):
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("stable\n", encoding="utf-8")
    runtime = make_runtime(tmp_path)
    basic_tools.run_read("a.py", runtime=runtime)
    basic_tools.run_read("b.py", runtime=runtime)

    def integrate(_name, _cleanup):
        (tmp_path / "a.py").write_text("worker change\n", encoding="utf-8")
        return json.dumps({
            "status": "integrated",
            "changed_files": ["a.py"],
        })

    monkeypatch.setattr(tool_handlers, "integrate_worktree", integrate)
    output = tool_handlers.run_integrate_worktree(
        "worker", runtime=runtime,
    )

    assert json.loads(output)["status"] == "integrated"
    assert runtime.state.knowledge.files["a.py"].evidence_valid is False
    assert runtime.state.knowledge.files["a.py"].version == 2
    assert runtime.state.knowledge.files["b.py"].evidence_valid is True
    assert runtime.state.knowledge.modified_files == {"a.py"}
