from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from codepilot_s20 import bootstrap

bootstrap()

from codepilot_s20 import agent_loop, subagent, task_system, worktree_system
from codepilot_s20.agent_profiles import assess_task_complexity
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
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        "glob", "read_file",
    }
    assert "write_file" not in {tool["name"] for tool in client.calls[0]["tools"]}


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


def test_complex_lead_requires_explorer_and_fresh_reviewer(tmp_path):
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
            {"role": "explorer", "prompt": "Map the contract and service path"},
            "explore",
        )),
        response(text_block(json.dumps({
            "verdict": "complete", "summary": "mapped",
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
        response(tool_block(
            "delegate_agent",
            {"role": "reviewer", "prompt": "Audit final service.py against README"},
            "review",
        )),
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
    assert len(client.calls) == 10
    lead_tools = [
        {tool["name"] for tool in call["tools"]}
        for call in client.calls
        if len(call["tools"]) == 30
    ]
    assert lead_tools and all("delegate_agent" in tools for tools in lead_tools)
