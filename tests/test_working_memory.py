from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from codepilot_s20 import (
    background,
    basic_tools,
    compact,
    context,
    prompts,
    tool_handlers,
)
from codepilot_s20.command_executor import LocalCommandExecutor
from codepilot_s20.runtime import AgentRuntime
from codepilot_s20.tool_defs import builtin_handlers


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
        "evidence_sources": {"files": ["a.py"]},
    }], runtime=runtime)
    assert knowledge.acceptance["accept:1"]["evidence_state"] == "verified"

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


def test_test_results_are_workspace_snapshots_not_coverage_claims(tmp_path):
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
    basic_tools.run_read("b.py", runtime=runtime)
    basic_tools.run_write("a.py", "value = 2\n", runtime=runtime)
    basic_tools.run_write("b.py", "other = 2\n", runtime=runtime)
    basic_tools.run_read("a.py", runtime=runtime)
    basic_tools.run_read("b.py", runtime=runtime)

    assert basic_tools.run_bash(
        "python -m pytest tests/test_a.py -q", runtime=runtime,
    ) == "12 passed"
    test = runtime.state.knowledge.recent_tests[-1]
    assert test.passed is True
    assert test.workspace_versions_at_run == {"a.py": 2, "b.py": 2}
    assert test.covered_source_versions == {}

    basic_tools.run_write("b.py", "other = 3\n", runtime=runtime)
    assert test.workspace_changed_since_run == ["b.py"]
    basic_tools.run_write("a.py", "value = 3\n", runtime=runtime)
    assert test.workspace_changed_since_run == ["b.py", "a.py"]


def test_foreground_bash_reconciles_actual_workspace_mutations(tmp_path):
    class MutatingExecutor:
        def execute(self, command, cwd, timeout):
            (cwd / "service.py").write_text(
                "def reserve():\n    return 2\n", encoding="utf-8",
            )
            return {
                "command": command,
                "exit_code": 0,
                "stdout": "changed",
                "stderr": "",
                "timed_out": False,
                "duration_ms": 5,
            }

    original = "def reserve():\n    return 1\n"
    (tmp_path / "service.py").write_text(original, encoding="utf-8")
    runtime = make_runtime(tmp_path, MutatingExecutor())
    basic_tools.run_read("service.py", runtime=runtime)
    runtime.state.knowledge.record_contract(
        "contract:reserve", "reserve contract", ["service.py"],
    )

    assert basic_tools.run_bash("custom-mutator", runtime=runtime) == "changed"

    record = runtime.state.knowledge.files["service.py"]
    assert record.version == 2
    assert record.digest == hashlib.sha256(
        (tmp_path / "service.py").read_bytes(),
    ).hexdigest()
    assert record.evidence_valid is False
    assert runtime.state.knowledge.confirmed_contracts[
        "contract:reserve"
    ].evidence_state == "stale"


def test_background_bash_uses_the_same_mutation_reconciliation(tmp_path):
    class MutatingExecutor:
        def execute(self, command, cwd, timeout):
            (cwd / "service.py").write_text(
                "value = 2\n", encoding="utf-8",
            )
            return {
                "command": command,
                "exit_code": 0,
                "stdout": "background changed",
                "stderr": "",
                "timed_out": False,
                "duration_ms": 5,
            }

    (tmp_path / "service.py").write_text("value = 1\n", encoding="utf-8")
    runtime = make_runtime(tmp_path, MutatingExecutor())
    basic_tools.run_read("service.py", runtime=runtime)
    background.background_tasks.clear()
    background.background_results.clear()
    block = SimpleNamespace(
        id="bg-mutation",
        name="bash",
        input={"command": "custom-mutator", "run_in_background": True},
    )

    background.start_background_task(block, builtin_handlers(runtime))
    assert background.wait_for_background_tasks(2.0) is True
    background.collect_background_results()

    record = runtime.state.knowledge.files["service.py"]
    assert record.version == 2
    assert record.evidence_valid is False


def test_concurrent_background_mutations_do_not_double_invalidate_versions(
    tmp_path,
):
    class MutatingExecutor:
        def execute(self, command, cwd, timeout):
            (cwd / f"{command}.py").write_text(
                "value = 2\n", encoding="utf-8",
            )
            time.sleep(0.02)
            return {
                "command": command,
                "exit_code": 0,
                "stdout": "changed",
                "stderr": "",
                "timed_out": False,
                "duration_ms": 20,
            }

    for name in ("a", "b"):
        (tmp_path / f"{name}.py").write_text(
            "value = 1\n", encoding="utf-8",
        )
    runtime = make_runtime(tmp_path, MutatingExecutor())
    for name in ("a", "b"):
        basic_tools.run_read(f"{name}.py", runtime=runtime)
    background.background_tasks.clear()
    background.background_results.clear()
    handlers = builtin_handlers(runtime)

    for name in ("a", "b"):
        background.start_background_task(
            SimpleNamespace(
                id=f"bg-{name}",
                name="bash",
                input={
                    "command": name,
                    "run_in_background": True,
                },
            ),
            handlers,
        )
    assert background.wait_for_background_tasks(2.0) is True
    background.collect_background_results()

    assert runtime.state.knowledge.files["a.py"].version == 2
    assert runtime.state.knowledge.files["b.py"].version == 2


def test_completed_acceptance_without_provenance_is_unbound(tmp_path):
    runtime = make_runtime(tmp_path)

    basic_tools.run_todo_write([{
        "id": "accept:1",
        "content": "service is correct",
        "status": "completed",
        "kind": "acceptance",
        "evidence": "looks good",
    }], runtime=runtime)

    item = runtime.state.knowledge.acceptance["accept:1"]
    assert item["evidence_state"] == "unbound"
    assert item["evidence_valid"] is False
    assert runtime.state.knowledge.confirmed_contracts[
        "acceptance:accept:1"
    ].evidence_state == "unbound"


def test_reread_does_not_revive_old_contract_acceptance_or_reviewer(tmp_path):
    (tmp_path / "service.py").write_text("value = 1\n", encoding="utf-8")
    runtime = make_runtime(tmp_path)
    knowledge = runtime.state.knowledge
    basic_tools.run_read("service.py", runtime=runtime)
    knowledge.record_contract(
        "contract:service", "service contract", ["service.py"],
    )
    knowledge.record_reviewer_findings([{
        "file": "service.py",
        "requirement": "service remains correct",
    }], revision=1)
    basic_tools.run_todo_write([{
        "id": "accept:1",
        "content": "service remains correct",
        "status": "completed",
        "kind": "acceptance",
        "evidence": "service.py inspected",
        "evidence_sources": {"files": ["service.py"]},
    }], runtime=runtime)

    basic_tools.run_write("service.py", "value = 2\n", runtime=runtime)
    basic_tools.run_read("service.py", runtime=runtime)

    assert knowledge.files["service.py"].evidence_valid is True
    assert knowledge.confirmed_contracts[
        "contract:service"
    ].evidence_state == "stale"
    assert knowledge.acceptance["accept:1"]["evidence_state"] == "stale"
    assert knowledge.reviewer_findings[
        "review:r1:f1"
    ].evidence_state == "stale"


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
