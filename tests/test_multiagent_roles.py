from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aqours_code import bootstrap

bootstrap()

from aqours_code import (
    agent_loop,
    autonomous,
    subagent,
    task_system,
    tool_handlers,
    worktree_system,
)
from aqours_code.agent_profiles import (
    assess_task_complexity,
    classify_delegation_intent,
)
from aqours_code.command_executor import LocalCommandExecutor
from aqours_code.runtime import AgentRuntime
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
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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


def test_persistent_teammate_idle_waits_for_explicit_shutdown(monkeypatch):
    class DelayedShutdown:
        def __init__(self):
            self.wait_calls = 0

        def wait(self, _interval):
            self.wait_calls += 1
            return self.wait_calls > (
                autonomous.IDLE_TIMEOUT // autonomous.IDLE_POLL_INTERVAL
            )

    stop_event = DelayedShutdown()
    monkeypatch.setattr(
        autonomous.BUS, "read_inbox", lambda _agent_name: [],
    )
    monkeypatch.setattr(autonomous, "scan_unclaimed_tasks", lambda: [])

    result = autonomous.idle_poll(
        "persistent", [], "persistent", "any-focus",
        stop_event=stop_event,
    )

    assert result == "shutdown"
    assert stop_event.wait_calls > (
        autonomous.IDLE_TIMEOUT // autonomous.IDLE_POLL_INTERVAL
    )


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
    )["role"] == "explore"
    assert classify_delegation_intent(
        "Implement the bounded adapter fix and update its tests"
    )["role"] == "general-purpose"
    assert classify_delegation_intent(
        "Audit the final changes for correctness and regression risk"
    )["role"] == "review"
    assert classify_delegation_intent(
        "Plan the migration steps and verification approach"
    )["role"] == "plan"
    assert classify_delegation_intent(
        "Summarize the tradeoff in the supplied evidence"
    )["role"] == "general-purpose"
    assert classify_delegation_intent(
        "分析相关调用路径并定位状态来源"
    )["role"] == "explore"


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
    assert result["role"] == "general-purpose"
    assert result["routed_from"] == "task"
    assert len(client.calls) == 1
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        "bash", "read_file", "write_file", "edit_file", "glob",
    }
    assert requests[0]["purpose"] == "subagent"
    assert requests[0]["agent_role"] == "general-purpose"
    assert any(
        event_type == "subagent_routed"
        and payload["agent_role"] == "general-purpose"
        for event_type, payload in events
    )


def test_legacy_task_respects_finalization_reserve(tmp_path, monkeypatch):
    client = BudgetedScriptedClient([], max_calls=4)
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "WORKDIR", tmp_path)

    result = json.loads(subagent.spawn_subagent(
        "Inspect the repository and locate the request handler"))

    assert result["status"] == "budget_reserved"
    assert result["role"] == "general-purpose"
    assert result["routed_from"] == "task"
    assert client.calls == []


def test_explore_role_enforces_unique_read_path_budget(tmp_path, monkeypatch):
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

    result = subagent.run_role_agent("explore", "Summarize the parts", tmp_path)

    assert result["verdict"] == "complete"
    tool_results = next(
        message["content"] for message in client.calls[0]["messages"]
        if isinstance(message.get("content"), list)
        and any(
            isinstance(item, dict) and item.get("tool_use_id") == "read-8"
            for item in message["content"]
        )
    )
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

    result = subagent.run_role_agent("review", "Audit the final code", tmp_path)

    assert result["verdict"] == "pass"
    assert set(result) == {
        "verdict", "summary", "findings", "files_checked", "missing_evidence",
    }
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {"read_file"}
    assert "write_file" not in {tool["name"] for tool in client.calls[0]["tools"]}


def test_plan_role_is_read_only(tmp_path, monkeypatch):
    client = ScriptedClient([response(text_block(json.dumps({
        "verdict": "complete",
        "summary": "ordered plan ready",
        "plan": ["inspect contract", "edit service", "run focused tests"],
        "risks": ["rollback behavior"],
        "files_checked": [],
    })))])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", "Plan the service change")

    result = subagent.run_role_agent("plan", "Produce a plan", tmp_path)

    assert result["verdict"] == "complete"
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        "glob", "read_file",
    }
    assert {"write_file", "edit_file", "bash"}.isdisjoint(
        tool["name"] for tool in client.calls[0]["tools"]
    )


def test_worker_is_not_a_temporary_subagent_profile():
    result = json.loads(subagent.delegate_agent(
        "worker", "Change the service in a Worktree",
    ))

    assert result["status"] == "error"
    assert "explore, plan, review, or general-purpose" in result["error"]


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

    result = subagent.run_role_agent("explore", "Map the code", tmp_path)

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

    result = subagent.run_role_agent("review", "Audit the final code", tmp_path)

    assert result["verdict"] == "pass"
    assert len(client.calls) == 3
    assert client.calls[2]["tools"] == []
    assert client.calls[2]["max_tokens"] == 5000
    assert len(client.calls[2]["messages"]) == 1
    assert "<synthesis>" in client.calls[2]["messages"][0]["content"]
    assert "<role_evidence>" in client.calls[2]["messages"][0]["content"]


def test_general_purpose_subagent_edits_without_team_task_or_worktree(
    tmp_path, monkeypatch,
):
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
        "general-purpose", "Change only VALUE to 2", name="value-helper",
    ))

    assert delegated["status"] == "completed"
    assert delegated["changed_files"] == ["service.py"]
    assert (workspace / "service.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert not list(tasks_dir.glob("task_*.json"))
    assert not worktrees_dir.exists()
    assert {"task_id", "worktree", "commit"}.isdisjoint(delegated)


def test_complex_lead_can_use_explorer_and_fresh_reviewer(tmp_path):
    (tmp_path / "README.md").write_text(
        "Contract: preserve the API and keep state atomic.\n", encoding="utf-8")
    (tmp_path / "service.py").write_text("BROKEN = True\n", encoding="utf-8")
    pending_todos = [
        {"content": "Fix service", "status": "in_progress"},
        {"content": "Run focused tests", "status": "pending"},
    ]
    completed_todos = [
        {"content": "Fix service", "status": "completed"},
        {"content": "Run focused tests", "status": "completed"},
    ]
    client = ScriptedClient([
        response(tool_block(
            "delegate_agent",
            {"role": "explore", "prompt": "Map the contract and code path"},
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
        response(tool_block(
            "delegate_agent",
            {
                "role": "review",
                "prompt": "Review final service.py against README.md",
            },
            "review",
        )),
        response(text_block(json.dumps({
            "verdict": "pass", "summary": "contract satisfied",
            "findings": [], "files_checked": ["README.md", "service.py"],
            "missing_evidence": [],
        }))),
        response(tool_block("todo_write", {"todos": completed_todos}, "complete")),
        response(text_block("completed after independent review")),
        response(text_block(
            "NO_CONCRETE_FAILURE_REPRODUCED\nfocused checkpoint test passed"
        )),
        response(text_block("completed after independent test audit")),
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

    assert result["final_answer"] == "completed after independent test audit"
    assert (tmp_path / "service.py").read_text(encoding="utf-8") == "FIXED = True\n"
    assert len(client.calls) == 10
    explorer_calls = [
        call for call in client.calls
        if "You are the explore role" in call["system"]
    ]
    assert len(explorer_calls) == 1
    assert explorer_calls[0]["max_tokens"] == 4000
    reviewer_calls = [
        call for call in client.calls
        if "You are the review role" in call["system"]
    ]
    assert len(reviewer_calls) == 1
    audit_calls = [
        call for call in client.calls
        if "You are the general-purpose role" in call["system"]
    ]
    assert len(audit_calls) == 1
    assert "independent test audit" in audit_calls[0]["messages"][0]["content"]


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
            {"role": "explore", "prompt": "Map the relevant code path"},
            "explore-1",
        )),
        response(text_block("Useful repository notes, but not JSON.")),
        response(text_block("Still not JSON after synthesis.")),
        response(tool_block(
            "delegate_agent",
            {"role": "explore", "prompt": "Repeat the same exploration"},
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
        if "You are the explore role" in call["system"]
    ]) == 2
    assert not any(
        "You are the review role" in call["system"]
        for call in client.calls
    )


def complex_implementation_task() -> str:
    return (
        "Implement an end-to-end atomic transaction rollback and idempotency "
        "consistency change across multiple files with state and security risks.\n"
        "- inspect the producer path\n- retain rollback semantics\n"
        "- update the adapter\n- verify the final state transition\n"
        + "Keep each cross-file change focused and maintainable. " * 8
    )
    assert len([
        call for call in client.calls
        if "You are the general-purpose role" in call["system"]
    ]) == 1


def test_invalid_reviewer_json_preserves_an_actionable_finding():
    result = subagent._parse_role_result(
        "**Critical issue:** state.py allows CANCELED -> CONFIRMED, which "
        "violates the terminal-state contract. More analysis follows...",
        "review",
    )

    assert result["invalid_json"] is True
    assert result["verdict"] == "gaps"
    assert result["findings"][0]["severity"] == "warning"
    assert result["findings"][0]["file"] == "state.py"
    assert "CANCELED" in result["findings"][0]["evidence"]


def test_reviewer_findings_do_not_mutate_todo_state(tmp_path):
    (tmp_path / "state.py").write_text(
        "CANCELED = {'CONFIRMED'}\n", encoding="utf-8")
    runtime = _review_runtime(tmp_path)
    runtime.state.todos.append({
        "id": "todo:1",
        "content": "Run transition tests",
        "status": "pending",
    })
    before = [dict(item) for item in runtime.state.todos]
    delegation = {
        "status": "completed",
        "verdict": "gaps",
        "result": {
            "verdict": "gaps",
            "summary": "terminal transition is invalid",
            "findings": [{
                "severity": "critical",
                "requirement": "Canceled reservations remain terminal",
                "file": "state.py",
                "symbol": "CANCELED",
                "evidence": "CANCELED currently allows CONFIRMED",
            }],
            "files_checked": ["state.py"],
            "missing_evidence": [],
        },
    }

    screening = agent_loop._screen_reviewer_findings(delegation, runtime)
    output = json.loads(
        agent_loop._screened_reviewer_output(delegation, screening)
    )

    assert runtime.state.todos == before
    assert len(output["result"]["findings"]) == 1
    assert output["result"]["findings"][0]["requirement"] == (
        "Canceled reservations remain terminal"
    )


def _review_runtime(tmp_path: Path) -> AgentRuntime:
    return AgentRuntime.create(
        workdir=tmp_path,
        model_client=SimpleNamespace(messages=object()),
        command_executor=LocalCommandExecutor(),
        model_provider="test",
        model="test-model",
        approval_mode="non_interactive",
        root_task="review test",
    )


def test_reviewer_finding_screen_validates_evidence_and_deduplicates(
    tmp_path, monkeypatch,
):
    (tmp_path / "service.py").write_text(
        "class Ledger:\n"
        "    def ingest(self):\n"
        "        receipt_snapshot = {}\n"
        "        batch_allocation = []\n"
        "        return receipt_snapshot, batch_allocation\n",
        encoding="utf-8",
    )
    runtime = _review_runtime(tmp_path)
    events = []
    monkeypatch.setattr(
        agent_loop, "record_event",
        lambda event, **data: events.append((event, data)),
    )
    delegation = {
        "status": "completed",
        "verdict": "gaps",
        "result": {
            "files_checked": ["service.py"],
            "findings": [
                {
                    "severity": "major",
                    "requirement": (
                        "Failed batch ingestion restores idempotency receipt "
                        "state exactly"
                    ),
                    "file": "service.py",
                    "symbol": "Ledger.ingest",
                    "evidence": (
                        "ingest snapshots allocation but does not restore the "
                        "batch receipt state"
                    ),
                },
                {
                    "severity": "critical",
                    "requirement": (
                        "Rollback after a batch exception must restore the "
                        "idempotency snapshot"
                    ),
                    "file": "service.py",
                    "symbol": "Ledger.ingest",
                    "evidence": (
                        "The batch allocation and receipt snapshot can diverge "
                        "during restore"
                    ),
                },
                {
                    "severity": "major",
                    "requirement": "Validate persisted duplicate event IDs",
                    "file": "validation.py",
                    "symbol": "normalize_events",
                    "evidence": "Only duplicates within the current list are checked",
                },
                {
                    "severity": "minor",
                    "requirement": "Receipt rollback should be atomic",
                    "file": "service.py",
                    "symbol": "Ledger.ingest",
                    "evidence": (
                        "The implementation is already correct and the rollback "
                        "is handled"
                    ),
                },
                {
                    "severity": "major",
                    "requirement": "Restore the checkpoint digest",
                    "file": "service.py",
                    "symbol": "missing_restore_handler",
                    "evidence": "The handler omits the saved digest",
                },
            ],
        },
    }

    screening = agent_loop._screen_reviewer_findings(delegation, runtime)

    assert len(screening["findings"]) == 1
    assert screening["findings"][0]["severity"] == "critical"
    assert {
        item["reason"] for item in screening["suppressed"]
    } == {
        "semantic_duplicate",
        "file_not_checked",
        "self_negating_evidence",
        "symbol_not_found",
    }
    assert events[-1][1]["decision"] == "reviewer_findings_screened"
    assert events[-1][1]["raw_count"] == 5
    assert events[-1][1]["accepted_count"] == 1
    assert len(events[-1][1]["suppressed_findings"]) == 4


def test_reviewer_finding_screen_keeps_grounded_unique_finding(tmp_path):
    (tmp_path / "checksum.py").write_text(
        "class Checkpoint:\n"
        "    def fingerprint(self, payload):\n"
        "        return payload['digest']\n",
        encoding="utf-8",
    )
    runtime = _review_runtime(tmp_path)
    screening = agent_loop._screen_reviewer_findings({
        "status": "completed",
        "verdict": "gaps",
        "result": {
            "files_checked": ["checksum.py"],
            "findings": [{
                "severity": "critical",
                "requirement": (
                    "The checkpoint fingerprint includes event sequences"
                ),
                "file": "checksum.py",
                "symbol": "Checkpoint.fingerprint",
                "evidence": (
                    "fingerprint returns only payload digest and omits sequences"
                ),
            }],
        },
    }, runtime)

    assert len(screening["findings"]) == 1
    assert screening["suppressed"] == []
    assert "_existing_todo_id" not in screening["findings"][0]


def test_reviewer_screen_suppresses_withdrawals_and_unsupported_absence(
    tmp_path,
):
    (tmp_path / "service.py").write_text(
        "class LedgerService:\n"
        "    def ingest(self):\n"
        "        return None\n",
        encoding="utf-8",
    )
    runtime = _review_runtime(tmp_path)
    screening = agent_loop._screen_reviewer_findings({
        "status": "completed",
        "verdict": "gaps",
        "result": {
            "files_checked": ["service.py"],
            "findings": [
                {
                    "severity": "critical",
                    "requirement": "Fingerprint includes event_id",
                    "file": "service.py",
                    "symbol": "LedgerService.ingest",
                    "evidence": (
                        "The field is present, so this is actually safe. Withdraw."
                    ),
                },
                {
                    "severity": "minor",
                    "requirement": "Rollback restores the receipt ID",
                    "file": "service.py",
                    "symbol": "LedgerService.ingest",
                    "evidence": (
                        "The restore is correct and the contract appears "
                        "satisfied. No defect."
                    ),
                },
                {
                    "severity": "critical",
                    "requirement": "Global event IDs are unique",
                    "file": "service.py",
                    "symbol": "LedgerService.ingest",
                    "evidence": (
                        "There is no evidence this is implemented anywhere in "
                        "the repository."
                    ),
                },
            ],
        },
    }, runtime)

    assert screening["findings"] == []
    assert [
        item["reason"] for item in screening["suppressed"]
    ] == [
        "self_negating_evidence",
        "self_negating_evidence",
        "unsupported_absence_claim",
    ]


def test_screened_reviewer_output_hides_suppressed_findings_from_lead(
    tmp_path,
):
    (tmp_path / "checksum.py").write_text(
        "def checkpoint_digest(document):\n"
        "    return document['balances']\n",
        encoding="utf-8",
    )
    delegation = {
        "status": "completed",
        "verdict": "gaps",
        "result": {
            "files_checked": ["checksum.py"],
            "findings": [
                {
                    "severity": "critical",
                    "requirement": "Digest includes sequences",
                    "file": "checksum.py",
                    "symbol": "checkpoint_digest",
                    "evidence": "The canonical payload omits sequences",
                },
                {
                    "severity": "minor",
                    "requirement": "Digest includes balances",
                    "file": "checksum.py",
                    "symbol": "checkpoint_digest",
                    "evidence": "Balances is present. Finding withdrawn.",
                },
            ],
        },
    }
    screening = agent_loop._screen_reviewer_findings(
        delegation, _review_runtime(tmp_path),
    )

    screened = json.loads(
        agent_loop._screened_reviewer_output(delegation, screening)
    )

    assert len(screened["result"]["findings"]) == 1
    assert screened["result"]["findings"][0]["requirement"] == (
        "Digest includes sequences"
    )
    assert "withdrawn" not in json.dumps(screened["result"]["findings"])
    assert screened["screening"]["raw_finding_count"] == 2
    assert screened["screening"]["suppressed_finding_count"] == 1


def test_readme_keywords_do_not_inject_hidden_todos(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Ledger\n\n"
        "## Exactly-once and idempotency\n\n"
        "Idempotency fingerprints include every normalized field: event ID, "
        "transaction ID, account ID, partition, sequence, delta, and normalized "
        "currency.\n\n"
        "## Atomic ingestion\n\n"
        "If validation, sequence checking, event insertion, or projection fails, "
        "event store, balances, partition sequences, receipts, idempotency "
        "bindings, and the next batch identifier all remain exactly unchanged.\n\n"
        "## Checkpoints and recovery\n\n"
        "`create_checkpoint()` captures fresh copies of balances, partition "
        "sequences, the ordered event IDs, and `event_count`. Its SHA-256 digest "
        "covers all four fields using canonical JSON.\n",
        encoding="utf-8",
    )
    trace_path = tmp_path / "trace.jsonl"
    client = ScriptedClient([
        response(tool_block("todo_write", {"todos": [{
            "id": "todo:lead",
            "content": "Lead-selected checkpoint requirement",
            "status": "completed",
        }]}, "todo")),
        response(text_block("complete without keyword injection")),
    ])

    result = agent_loop.run_agent_task(
        "Fix the README contract and run tests.",
        str(tmp_path),
        str(trace_path),
        model_client=client,
        model_provider="scripted",
        model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "complete without keyword injection"
    assert len(client.calls) == 2
    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert not any(
        event.get("decision") == "readme_contract_sections_registered"
        for event in events
    )


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


def test_complex_task_starts_one_general_purpose_test_audit(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    trace_path = tmp_path / "trace.jsonl"
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
        response(text_block(
            "Contracts: atomic rollback.\n"
            "Command: python /tmp/aqours_test_audit_1/test_service.py\n"
            "PASS\nNO_CONCRETE_FAILURE_REPRODUCED"
        )),
        response(text_block("finished after audit")),
    ], max_calls=20)

    result = agent_loop.run_agent_task(
        task, str(tmp_path), str(trace_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "finished after audit"
    assert len(client.calls) == 4
    assert all(
        any(tool["name"] == "delegate_agent" for tool in call["tools"])
        for call in (client.calls[0], client.calls[-1])
    )
    assert not any(
        "You are the review role" in call["system"]
        for call in client.calls
    )
    audit_calls = [
        call for call in client.calls
        if "You are the general-purpose role" in call["system"]
    ]
    assert len(audit_calls) == 1
    assignment = audit_calls[0]["messages"][0]["content"]
    assert task in assignment
    assert "service.py" in assignment
    assert "/tmp/aqours_test_audit_*" in assignment
    assert "git diff --stat" not in assignment or "current_git_diff_stat" in assignment
    assert any(
        "python /tmp/aqours_test_audit_1/test_service.py" in str(
            message.get("content")
        )
        for message in client.calls[-1]["messages"]
    )
    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event["type"] == "subagent_start"
        and event["agent_role"] == "general-purpose"
        and event["name"] == "test-audit"
        for event in events
    )
    started = [event for event in events if event["type"] == "test_audit_started"]
    completed = [
        event for event in events if event["type"] == "test_audit_completed"
    ]
    assert len(started) == len(completed) == 1
    assert started[0]["mutation_revision"] == 1
    assert completed[0]["status"] == "completed"
    assert completed[0]["model_calls"] == 1
    assert completed[0]["changed_files"] == []


def test_test_audit_uses_shared_global_budget_beyond_six_tool_rounds(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    for index in range(8):
        (tmp_path / f"audit_{index}.py").write_text(
            f"VALUE = {index}\n", encoding="utf-8",
        )
    trace_path = tmp_path / "trace.jsonl"
    audit_tool_rounds = [
        response(tool_block(
            "read_file", {"path": f"audit_{index}.py"}, f"audit-read-{index}",
        ))
        for index in range(8)
    ]
    report = (
        "Command: python /tmp/aqours_test_audit_long/test_service.py\n"
        "PASS\nNO_CONCRETE_FAILURE_REPRODUCED"
    )
    client = BudgetedScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(text_block("ready for final")),
        *audit_tool_rounds,
        response(text_block(report)),
        response(text_block("finished after long audit")),
    ], max_calls=64)

    result = agent_loop.run_agent_task(
        complex_implementation_task(), str(tmp_path), str(trace_path),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "finished after long audit"
    audit_calls = [
        call for call in client.calls
        if "You are the general-purpose role" in call["system"]
    ]
    assert len(audit_calls) == 9
    assert len(client.calls) == 12
    lead_audit_context = "\n".join(
        str(message.get("content"))
        for message in client.calls[-1]["messages"]
    )
    assert "aqours_test_audit_long/test_service.py" in lead_audit_context
    assert "NO_CONCRETE_FAILURE_REPRODUCED" in lead_audit_context
    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    completed = [
        event for event in events if event["type"] == "test_audit_completed"
    ]
    assert completed[-1]["model_calls"] == 9
    assert client.budget_snapshot() == {
        "max_calls": 64,
        "call_count": 12,
        "max_provider_retries": 0,
    }


def test_regular_general_purpose_keeps_six_tool_round_limit(
    tmp_path, monkeypatch,
):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedClient([
        *[
            response(tool_block(
                "read_file", {"path": "service.py", "offset": index},
                f"read-{index}",
            ))
            for index in range(6)
        ],
        response(text_block(json.dumps({
            "verdict": "blocked",
            "summary": "stopped after the ordinary six tool rounds",
            "changed_files": [],
            "tests": [],
            "remaining_risks": ["more inspection was requested"],
        }))),
    ])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", "Inspect service.py")

    result = json.loads(subagent.delegate_agent(
        "general-purpose", "Inspect service.py", name="ordinary-agent",
    ))

    assert result["status"] == "completed"
    assert len(client.calls) == 7
    assert all(call["tools"] for call in client.calls[:6])
    assert client.calls[6]["tools"] == []
    assert any(
        "<synthesis>" in str(message.get("content"))
        for message in client.calls[6]["messages"]
    )


@pytest.mark.parametrize(
    ("task", "edit_workspace"),
    [
        ("What does this function do?", True),
        (complex_implementation_task(), False),
        (
            "Write a detailed non-code project retrospective with risks, "
            "requirements, tests, atomic state, concurrency, and rollback. " * 8,
            True,
        ),
    ],
    ids=("simple_question", "no_workspace_change", "non_code_task"),
)
def test_test_audit_does_not_run_without_all_triggers(
    tmp_path, task, edit_workspace,
):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    responses = []
    if edit_workspace:
        responses.append(response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )))
    responses.append(response(text_block("original final")))
    client = ScriptedClient(responses)

    result = agent_loop.run_agent_task(
        task, str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "original final"
    assert not any(
        "You are the general-purpose role" in call["system"]
        for call in client.calls
    )


def test_test_audit_budget_skip_keeps_original_final_and_traces_reason(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    trace_path = tmp_path / "trace.jsonl"
    client = BudgetedScriptedClient([
        response(tool_block("todo_write", {"todos": [{
            "content": "still pending",
            "status": "pending",
        }]}, "todo")),
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(text_block("original final")),
    ], max_calls=11)

    result = agent_loop.run_agent_task(
        complex_implementation_task(), str(tmp_path), str(trace_path),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "original final"
    assert len(client.calls) == 3
    assert not any(
        "todo_completion_reminder" in str(message.get("content"))
        for call in client.calls for message in call["messages"]
    )
    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    skipped = [event for event in events if event["type"] == "test_audit_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "budget_reserved"


@pytest.mark.parametrize("failure", [TimeoutError("late"), RuntimeError("provider")])
def test_test_audit_failure_keeps_original_final(tmp_path, failure):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    trace_path = tmp_path / "trace.jsonl"
    client = ScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(text_block("original final")),
        failure,
    ])

    result = agent_loop.run_agent_task(
        complex_implementation_task(), str(tmp_path), str(trace_path),
        model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "original final"
    assert len(client.calls) == 3
    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    completed = [event for event in events if event["type"] == "test_audit_completed"]
    assert completed[-1]["status"] == "inconclusive"


def test_test_audit_invalid_json_raw_summary_reaches_lead_without_synthesis(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    raw_report = (
        "<audit>command: python /tmp/aqours_test_audit_x/test_service.py\n"
        "AssertionError: expected persisted state</audit>"
    )
    client = ScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(text_block("first final")),
        response(text_block(raw_report)),
        response(text_block("second final")),
    ])

    result = agent_loop.run_agent_task(
        complex_implementation_task(), str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "second final"
    assert len(client.calls) == 4
    audit_context = "\n".join(
        str(message.get("content"))
        for message in client.calls[-1]["messages"]
    )
    assert "python /tmp/aqours_test_audit_x/test_service.py" in audit_context
    assert "AssertionError: expected persisted state" in audit_context


def test_test_audit_error_with_raw_summary_still_reaches_lead(tmp_path, monkeypatch):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(text_block("first final")),
        response(text_block("second final")),
    ])
    original_delegate = subagent.delegate_agent

    def raw_error_delegate(role, prompt, name="", runtime=None):
        if name == "test-audit":
            return json.dumps({
                "status": "error",
                "role": "general-purpose",
                "verdict": "blocked",
                "error": "provider response was invalid",
                "result": {
                    "summary": (
                        "command: python /tmp/aqours_test_audit_raw/test_service.py\n"
                        "AssertionError: persisted state mismatch"
                    ),
                    "invalid_json": True,
                },
                "changed_files": [],
            })
        return original_delegate(role, prompt, name, runtime)

    monkeypatch.setattr(subagent, "delegate_agent", raw_error_delegate)
    monkeypatch.setattr(agent_loop, "delegate_agent", raw_error_delegate)
    monkeypatch.setattr(tool_handlers, "delegate_agent", raw_error_delegate)

    result = agent_loop.run_agent_task(
        complex_implementation_task(), str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "second final"
    audit_context = "\n".join(
        str(message.get("content"))
        for message in client.calls[-1]["messages"]
    )
    assert "aqours_test_audit_raw/test_service.py" in audit_context
    assert "persisted state mismatch" in audit_context


def test_test_audit_runs_only_once_after_lead_edits_again(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit-1",
        )),
        response(text_block("first final")),
        response(text_block(
            "python /tmp/aqours_test_audit_x/test_service.py failed: "
            "AssertionError: expected 3"
        )),
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 2", "new_text": "VALUE = 3"},
            "edit-2",
        )),
        response(text_block("final after fix")),
    ])

    result = agent_loop.run_agent_task(
        complex_implementation_task(), str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "final after fix"
    assert (tmp_path / "service.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert len([
        call for call in client.calls
        if "You are the general-purpose role" in call["system"]
    ]) == 1


def test_test_audit_workspace_change_is_warned_without_restore(tmp_path, monkeypatch):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(text_block("first final")),
        response(text_block("final after warning")),
    ])
    original_delegate = subagent.delegate_agent

    def changing_delegate(role, prompt, name="", runtime=None):
        if name == "test-audit":
            (tmp_path / "audit_wrote.txt").write_text("unexpected\n", encoding="utf-8")
            return json.dumps({
                "status": "completed",
                "role": "general-purpose",
                "verdict": "blocked",
                "result": {
                    "verdict": "blocked",
                    "summary": "focused test was inconclusive",
                    "invalid_json": True,
                },
                "changed_files": ["audit_wrote.txt"],
            })
        return original_delegate(role, prompt, name, runtime)

    monkeypatch.setattr(subagent, "delegate_agent", changing_delegate)
    monkeypatch.setattr(agent_loop, "delegate_agent", changing_delegate)
    monkeypatch.setattr(tool_handlers, "delegate_agent", changing_delegate)

    result = agent_loop.run_agent_task(
        complex_implementation_task(), str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "final after warning"
    assert (tmp_path / "audit_wrote.txt").read_text(encoding="utf-8") == "unexpected\n"
    assert any(
        "WARNING: The test audit unexpectedly modified repository files" in str(
            message.get("content")
        )
        and "audit_wrote.txt" in str(message.get("content"))
        for message in client.calls[-1]["messages"]
    )


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


def test_reviewer_gap_stays_in_tool_output_without_hidden_todo(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    task = (
        "Implement an end-to-end atomic transaction rollback and idempotency "
        "consistency change across multiple files with state and security risks.\n"
        "- inspect the producer path\n- retain rollback semantics\n"
        "- update the adapter\n- verify the final state transition\n"
        + "Keep each cross-file change focused and maintainable. " * 8
    )
    client = ScriptedClient([
        response(tool_block(
            "edit_file",
            {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            "edit",
        )),
        response(tool_block(
            "delegate_agent",
            {
                "role": "review",
                "prompt": "Review service.py rollback behavior",
            },
            "review",
        )),
        response(text_block(json.dumps({
            "verdict": "gaps",
            "summary": "rollback is incomplete",
            "findings": [{
                "severity": "critical",
                "requirement": "Failed reservations must roll back",
                "file": "service.py",
                "symbol": "VALUE",
                "evidence": "A later failure leaves the first deduction applied.",
            }],
            "files_checked": ["service.py"],
            "missing_evidence": [],
        }))),
        response(text_block("finished with the reviewer concern reported")),
        response(text_block(
            "NO_CONCRETE_FAILURE_REPRODUCED\nfocused rollback test passed"
        )),
        response(text_block("finished after test audit")),
    ])

    result = agent_loop.run_agent_task(
        task, str(tmp_path), model_client=client,
        model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "finished after test audit"
    assert len(client.calls) == 6
    assert any(
        "rollback is incomplete" in str(message.get("content"))
        for message in client.calls[-1]["messages"]
    )
    assert not any(
        "todo_completion_reminder" in str(message.get("content"))
        for call in client.calls for message in call["messages"]
    )
