from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from codepilot_s20 import bootstrap

bootstrap()

from codepilot_s20 import agent_loop, subagent, task_system, worktree_system
from codepilot_s20.agent_profiles import (
    assess_task_complexity,
    classify_delegation_intent,
)
from evals import run_eval


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, data: dict, block_id: str):
    return SimpleNamespace(type="tool_use", name=name, input=data, id=block_id)


def response(*blocks):
    has_tool = any(block.type == "tool_use" for block in blocks)
    return SimpleNamespace(
        content=list(blocks),
        stop_reason="tool_use" if has_tool else "end_turn",
    )


class ScriptedClient:
    def __init__(self, responses):
        self.messages = self
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class BudgetedScriptedClient(ScriptedClient):
    def __init__(self, responses, max_calls: int):
        super().__init__(responses)
        self.max_calls = max_calls

    def budget_snapshot(self):
        return {
            "max_calls": self.max_calls,
            "call_count": len(self.calls),
            "max_provider_retries": 0,
        }


def run_git(cwd: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    )


def test_complexity_assessment_is_generic_and_deterministic():
    assert assess_task_complexity("Fix a typo")["level"] == "simple"
    explanation = assess_task_complexity(
        "Explain the README contract, atomic concurrency, transaction rollback, "
        "idempotency consistency, regression tests, and end-to-end repository risks."
    )
    assert explanation["level"] == "complex"
    assert explanation["implementation_task"] is False
    task = """
Implement an end-to-end repository change from the README contract.
- Preserve the public API and compatibility.
- Keep reservation atomic under concurrent requests.
- Enforce idempotency and consistency on every error path.
- Add tests and run the regression suite to verify behavior.
"""
    assessment = assess_task_complexity(task)
    assert assessment["level"] == "complex"
    assert "cross_cutting_risk" in assessment["reasons"]


def test_delegation_intent_routes_by_work_type_not_case_identity():
    assert classify_delegation_intent(
        "Read the repository guidance and map the producer path"
    )["role"] == "explorer"
    assert classify_delegation_intent(
        "Implement the bounded adapter fix and update its tests"
    )["role"] == "worker"
    assert classify_delegation_intent(
        "Audit the final changes for correctness and regression risk"
    )["role"] == "reviewer"
    assert classify_delegation_intent(
        "Summarize the tradeoff in the supplied evidence"
    )["role"] == "general"
    assert classify_delegation_intent(
        "分析相关调用路径并定位状态来源"
    )["role"] == "explorer"


def test_legacy_task_uses_bounded_traced_role_runtime(tmp_path, monkeypatch):
    requests = []
    events = []
    client = ScriptedClient([response(text_block(json.dumps({
        "verdict": "complete",
        "summary": "mapped the relevant path",
        "requirements": [],
        "code_map": ["service.py"],
        "risks": [],
        "files_checked": ["service.py"],
    })))])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "WORKDIR", tmp_path)
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", "Understand this repository")
    monkeypatch.setattr(
        subagent, "record_llm_request", lambda **payload: requests.append(payload))
    monkeypatch.setattr(
        subagent, "record_event",
        lambda event_type, **payload: events.append((event_type, payload)),
    )

    result = json.loads(subagent.spawn_subagent(
        "Read service.py and map the relevant execution path"))

    assert result["status"] == "completed"
    assert result["role"] == "explorer"
    assert result["routed_from"] == "task"
    assert len(client.calls) == 1
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {"read_file"}
    assert requests[0]["purpose"] == "delegate_agent"
    assert requests[0]["agent_role"] == "explorer"
    assert any(
        event_type == "delegation_routed"
        and payload["agent_role"] == "explorer"
        for event_type, payload in events
    )


def test_legacy_task_respects_finalization_reserve(tmp_path, monkeypatch):
    client = BudgetedScriptedClient([], max_calls=4)
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "WORKDIR", tmp_path)

    result = json.loads(subagent.spawn_subagent(
        "Inspect the repository and locate the request handler"))

    assert result["status"] == "budget_reserved"
    assert result["role"] == "explorer"
    assert result["routed_from"] == "task"
    assert client.calls == []


def test_general_role_enforces_unique_read_path_budget(tmp_path, monkeypatch):
    for index in range(9):
        (tmp_path / f"part_{index}.py").write_text(
            f"VALUE = {index}\n", encoding="utf-8")
    reads = [
        tool_block("read_file", {"path": f"part_{index}.py"}, f"read-{index}")
        for index in range(9)
    ]
    client = ScriptedClient([
        response(*reads),
        response(text_block(json.dumps({
            "verdict": "complete",
            "summary": "used the bounded evidence",
            "evidence": [],
            "files_checked": [f"part_{index}.py" for index in range(8)],
            "remaining_questions": ["part_8.py was outside the path budget"],
        }))),
    ])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", "Answer one focused question")

    result = subagent.run_role_agent("general", "Summarize the parts", tmp_path)

    assert result["verdict"] == "complete"
    tool_results = client.calls[1]["messages"][-2]["content"]
    ninth = next(
        item for item in tool_results if item["tool_use_id"] == "read-8")
    assert "8-path read budget" in ninth["content"]


def test_reviewer_role_is_actually_read_only(tmp_path, monkeypatch):
    client = ScriptedClient([response(text_block(json.dumps({
        "verdict": "pass",
        "summary": "contract satisfied",
        "findings": [],
        "files_checked": ["service.py"],
        "missing_evidence": [],
    })))])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", "Review the service contract")

    result = subagent.run_role_agent("reviewer", "Audit the final code", tmp_path)

    assert result["verdict"] == "pass"
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {"read_file"}
    assert "write_file" not in {tool["name"] for tool in client.calls[0]["tools"]}


def test_explorer_uses_manifest_and_harness_verified_files_checked(
    tmp_path, monkeypatch,
):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedClient([
        response(
            tool_block("read_file", {"path": "service.py"}, "read-source"),
            tool_block("read_file", {"path": "missing.py"}, "read-missing"),
        ),
        response(text_block(json.dumps({
            "verdict": "complete", "summary": "mapped",
            "requirements": ["preserve behavior"],
            "code_map": ["service.py:VALUE"], "risks": [],
            "files_checked": ["service.py", "missing.py", "invented.py"],
        }))),
    ])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", "Inspect the implementation")

    result = subagent.run_role_agent("explorer", "Map the code", tmp_path)

    assert result["verdict"] == "complete"
    assert result["files_checked"] == ["service.py"]
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {"read_file"}
    assert "<repository_manifest>" in client.calls[0]["system"]
    assert "service.py" in client.calls[0]["system"]


def test_role_tool_budget_reserves_a_no_tool_synthesis_turn(tmp_path, monkeypatch):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedClient([
        response(tool_block("read_file", {"path": "service.py"}, "read-1")),
        response(tool_block("read_file", {"path": "service.py"}, "read-2")),
        response(text_block(json.dumps({
            "verdict": "pass", "summary": "reviewed after tool budget",
            "findings": [], "files_checked": ["service.py"],
            "missing_evidence": [],
        }))),
    ])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", "Review service.py")

    result = subagent.run_role_agent("reviewer", "Audit the final code", tmp_path)

    assert result["verdict"] == "pass"
    assert len(client.calls) == 3
    assert client.calls[2]["tools"] == []
    assert len(client.calls[2]["messages"]) == 1
    assert "<synthesis>" in client.calls[2]["messages"][0]["content"]
    assert "<role_evidence>" in client.calls[2]["messages"][0]["content"]


def test_worker_changes_require_explicit_worktree_integration(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    (workspace / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_git(workspace, "init")
    run_git(workspace, "config", "user.name", "Test")
    run_git(workspace, "config", "user.email", "test@example.com")
    run_git(workspace, "add", "service.py")
    run_git(workspace, "commit", "-m", "baseline")

    tasks_dir = state / ".tasks"
    worktrees_dir = state / ".worktrees"
    monkeypatch.setattr(task_system, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(worktree_system, "WORKDIR", workspace)
    monkeypatch.setattr(worktree_system, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(subagent, "WORKDIR", workspace)
    monkeypatch.setattr(subagent, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", "Change VALUE to 2")

    client = ScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit-1",
        )),
        response(text_block(json.dumps({
            "verdict": "changes_ready",
            "summary": "updated value",
            "changed_files": ["service.py"],
            "tests": [],
            "remaining_risks": [],
        }))),
    ])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")

    delegated = json.loads(subagent.delegate_agent(
        "worker", "Change only VALUE to 2", name="value-worker",
    ))

    assert delegated["status"] == "changes_ready"
    assert delegated["changed_files"] == ["service.py"]
    assert (workspace / "service.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert delegated["commit"]

    (workspace / "service.py").write_text("VALUE = 3\n", encoding="utf-8")
    conflict = json.loads(worktree_system.integrate_worktree("value-worker"))
    assert conflict["status"] == "conflict"
    assert conflict["overlapping_files"] == ["service.py"]
    assert (worktrees_dir / "value-worker").exists()
    assert (workspace / "service.py").read_text(encoding="utf-8") == "VALUE = 3\n"

    (workspace / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    integrated = json.loads(worktree_system.integrate_worktree("value-worker"))
    assert integrated["status"] == "integrated"
    assert integrated["changed_files"] == ["service.py"]
    assert (workspace / "service.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert not (worktrees_dir / "value-worker").exists()


def test_complex_lead_can_use_explorer_and_fresh_reviewer(tmp_path):
    (tmp_path / "README.md").write_text(
        "Contract: preserve the API and keep state atomic.\n", encoding="utf-8")
    (tmp_path / "service.py").write_text("BROKEN = True\n", encoding="utf-8")
    pending_todos = [
        {"content": "Fix service", "status": "in_progress", "kind": "plan"},
        {"content": "Preserve API and atomic state", "status": "pending",
         "kind": "acceptance"},
    ]
    completed_todos = [
        {"content": "Fix service", "status": "completed", "kind": "plan"},
        {"content": "Preserve API and atomic state", "status": "completed",
         "kind": "acceptance", "evidence": "reviewer pass on final service.py"},
    ]
    client = ScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "BROKEN", "new_text": "FIXED"},
            "too-early",
        )),
        response(tool_block(
            "delegate_agent",
            {"role": "explorer", "prompt": "Map the contract and code path"},
            "explore",
        )),
        response(text_block(json.dumps({
            "verdict": "complete", "summary": "mapped from focused evidence",
            "requirements": ["preserve API", "atomic state"],
            "code_map": ["service.py"], "risks": ["rollback"],
            "files_checked": ["README.md", "service.py"],
        }))),
        response(tool_block("todo_write", {"todos": pending_todos}, "plan")),
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "BROKEN", "new_text": "FIXED"},
            "edit",
        )),
        response(text_block("done too early")),
        response(text_block(json.dumps({
            "verdict": "pass", "summary": "contract satisfied",
            "findings": [], "files_checked": ["README.md", "service.py"],
            "missing_evidence": [],
        }))),
        response(tool_block("todo_write", {"todos": completed_todos}, "evidence")),
        response(text_block("completed after independent review")),
    ])
    task = (
        "Implement an end-to-end repository change from the README contract. "
        "Preserve the public API and compatibility. Fix atomic concurrent "
        "transaction rollback, idempotency, and consistency on every error path. "
        "Run tests and the regression suite to verify behavior."
    )

    result = agent_loop.run_agent_task(
        task, str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "completed after independent review"
    assert (tmp_path / "service.py").read_text(encoding="utf-8") == "FIXED = True\n"
    assert len(client.calls) == 9
    explorer_calls = [
        call for call in client.calls
        if "You are the explorer role" in call["system"]
    ]
    assert len(explorer_calls) == 1
    assert explorer_calls[0]["max_tokens"] == 4000
    lead_tools = [
        {tool["name"] for tool in call["tools"]}
        for call in client.calls
        if len(call["tools"]) == 30
    ]
    assert lead_tools and all("delegate_agent" in tools for tools in lead_tools)
    reviewer_calls = [
        call for call in client.calls
        if "You are the reviewer role" in call["system"]
    ]
    assert len(reviewer_calls) == 1
    assert any(
        "<reviewer_result>" in str(message.get("content"))
        for call in client.calls for message in call["messages"]
    )


def test_inconclusive_explorer_is_reused_and_does_not_lock_lead(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    task = (
        "Implement an end-to-end repository update for atomic concurrent "
        "transaction rollback, idempotency, state migration, security, and race "
        "handling across multiple files.\n"
        "- inspect the producer\n- inspect the consumer\n- update the adapter\n"
        "- verify the final transition\n"
        + "Keep the implementation focused and maintainable. " * 12
    )
    assert assess_task_complexity(task)["level"] == "complex"
    client = ScriptedClient([
        response(tool_block(
            "delegate_agent",
            {"role": "explorer", "prompt": "Map the relevant code path"},
            "explore-1",
        )),
        response(text_block("Useful repository notes, but not JSON.")),
        response(text_block("Still not JSON after synthesis.")),
        response(tool_block(
            "delegate_agent",
            {"role": "explorer", "prompt": "Repeat the same exploration"},
            "explore-2",
        )),
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(text_block("final attempt")),
        response(text_block(json.dumps({
            "verdict": "pass", "summary": "lead change is consistent",
            "findings": [], "files_checked": ["service.py"],
            "missing_evidence": [],
        }))),
        response(text_block("finished from lead evidence")),
    ])

    result = agent_loop.run_agent_task(
        task, str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "finished from lead evidence"
    assert (tmp_path / "service.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert len(client.calls) == 8
    assert client.calls[2]["tools"] == []
    assert len([
        call for call in client.calls
        if "You are the explorer role" in call["system"]
    ]) == 2


def test_invalid_reviewer_json_preserves_an_actionable_finding():
    result = subagent._parse_role_result(
        "**Critical issue:** state.py allows CANCELED -> CONFIRMED, which "
        "violates the terminal-state contract. More analysis follows...",
        "reviewer",
    )

    assert result["invalid_json"] is True
    assert result["verdict"] == "gaps"
    assert result["findings"][0]["severity"] == "warning"
    assert result["findings"][0]["file"] == "state.py"
    assert "CANCELED" in result["findings"][0]["evidence"]


def test_reviewer_findings_become_locked_acceptance_work():
    from codepilot_s20 import basic_tools

    basic_tools.CURRENT_TODOS.clear()
    try:
        basic_tools.CURRENT_TODOS.extend([
            {"content": "Original contract", "status": "completed",
             "kind": "acceptance", "evidence": "existing evidence"},
        ])
        content = agent_loop._register_reviewer_findings([{
            "severity": "critical",
            "requirement": "Canceled reservations remain terminal",
            "file": "state.py",
            "symbol": "_ALLOWED_TARGETS",
            "evidence": "CANCELED currently allows CONFIRMED",
        }], revision=3)

        assert content.startswith(
            "Resolve pre-final reviewer finding for revision 3:")
        assert "state.py:_ALLOWED_TARGETS" in content
        assert basic_tools.CURRENT_TODOS[-1] == {
            "id": "review:r3:f1",
            "content": content,
            "status": "pending",
            "kind": "acceptance",
        }

        updated = basic_tools.run_todo_write([
            {"content": f"contract {index}", "status": "pending",
             "kind": "acceptance"}
            for index in range(11)
        ] + [{
            "id": "review:r3:f1",
            "content": "rewritten finding text must not replace identity",
            "kind": "plan",
            "status": "completed",
            "evidence": "state.py transition table and focused test prove terminal behavior",
        }])
        assert updated.startswith("Updated 12 todos")
        reviewer_item = next(
            item for item in basic_tools.CURRENT_TODOS
            if item.get("id") == "review:r3:f1")
        assert reviewer_item["content"] == content
        assert reviewer_item["kind"] == "acceptance"
        assert reviewer_item["status"] == "completed"
        assert "focused test" in reviewer_item["evidence"]
    finally:
        basic_tools.CURRENT_TODOS.clear()


def test_runtime_role_signal_requires_repeat_cross_scope_and_tail_budget():
    class Budget:
        def __init__(self, used):
            self.used = used

        def budget_snapshot(self):
            return {
                "source": "test", "max_calls": 40,
                "call_count": self.used, "max_provider_retries": 1,
            }

    broad_once = {
        "README.md": 1,
        **{f"src/module_{index}.py": 1 for index in range(6)},
        "tests/test_contract.py": 1,
    }
    no_repeat = agent_loop._runtime_role_benefit(broad_once, Budget(10))
    assert no_repeat["evidence_ready"] is False

    repeated = dict(broad_once)
    repeated["src/module_1.py"] = 2
    repeated["tests/test_contract.py"] = 2
    eligible = agent_loop._runtime_role_benefit(repeated, Budget(10))
    assert eligible["eligible"] is True
    assert eligible["repeated_reads"] == 2
    assert eligible["scope_count"] == 3

    tail = agent_loop._runtime_role_benefit(repeated, Budget(28))
    assert tail["evidence_ready"] is True
    assert tail["eligible"] is False
    assert tail["budget_allowed"] is False

    absolute = {
        "/workspace/README.md": 1,
        **{f"/workspace/src/module_{index}.py": 1 for index in range(6)},
        "/workspace/tests/test_contract.py": 3,
    }
    docker_paths = agent_loop._runtime_role_benefit(absolute, Budget(10))
    assert docker_paths["eligible"] is True
    assert docker_paths["scope_count"] == 3


def test_pre_final_reviewer_is_skipped_before_consuming_tail_reserve(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    task = (
        "Implement an end-to-end atomic transaction rollback and idempotency "
        "consistency change across multiple files with state and security risks.\n"
            "- inspect the producer path\n- retain rollback semantics\n"
        "- update the adapter\n- verify the final state transition\n"
        + "Keep each cross-file change focused and maintainable. " * 8
    )
    assert assess_task_complexity(task)["level"] == "complex"
    client = BudgetedScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(text_block("ready for final")),
        response(text_block("finished with reserved calls")),
    ], max_calls=7)

    result = agent_loop.run_agent_task(
        task, str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "ready for final"
    assert len(client.calls) == 2
    assert all(len(call["tools"]) == 30 for call in client.calls)


def test_last_budget_call_is_forced_to_be_a_tool_free_final(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = BudgetedScriptedClient([
        response(tool_block(
            "read_file", {"path": "service.py"}, "read-once")),
        response(text_block("final from retained evidence")),
    ], max_calls=2)

    result = agent_loop.run_agent_task(
        "Inspect service.py and report its current value.",
        str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "final from retained evidence"
    assert len(client.calls) == 2
    assert client.calls[1]["tools"] == []
    assert any(
        "<finalization_deadline>" in str(message.get("content"))
        for message in client.calls[1]["messages"]
    )


def test_reviewer_gap_creates_acceptance_even_when_classifier_missed_it(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    task = (
        "Implement an end-to-end atomic transaction rollback and idempotency "
        "consistency change across multiple files with state and security risks.\n"
        "- inspect the producer path\n- retain rollback semantics\n"
        "- update the adapter\n- verify the final state transition\n"
        + "Keep each cross-file change focused and maintainable. " * 8
    )
    finding_content = (
        "Resolve pre-final reviewer findings for revision 1: "
        "service.py:reserve Failed reservations must roll back"
    )
    client = ScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(text_block("ready for review")),
        response(text_block(json.dumps({
            "verdict": "gaps",
            "summary": "rollback is incomplete",
            "findings": [{
                "severity": "critical",
                "requirement": "Failed reservations must roll back",
                "file": "service.py",
                "symbol": "reserve",
                "evidence": "A later failure leaves the first deduction applied.",
            }],
            "files_checked": ["service.py"],
            "missing_evidence": [],
        }))),
        response(tool_block("todo_write", {"todos": [{
            "content": finding_content,
            "status": "completed",
            "kind": "acceptance",
            "evidence": "Lead inspected reserve and rejected the stale concern",
        }]}, "finding-evidence")),
        response(text_block("finished after addressing reviewer finding")),
    ])

    result = agent_loop.run_agent_task(
        task, str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "finished after addressing reviewer finding"
    assert any(
        finding_content in str(message.get("content"))
        for call in client.calls for message in call["messages"]
    )
