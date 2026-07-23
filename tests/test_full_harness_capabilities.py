from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from codepilot_s20 import bootstrap

bootstrap()

from codepilot_s20 import (
    agent_loop,
    background,
    basic_tools,
    context,
    mcp,
    prompts,
    protocol,
    skills,
    subagent,
    task_system,
    teammate,
    worktree_system,
)
from codepilot_s20.command_executor import LocalCommandExecutor
from evals import run_eval


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, data: dict, block_id: str):
    return SimpleNamespace(type="tool_use", name=name, input=data, id=block_id)


def response(*blocks):
    has_tool = any(block.type == "tool_use" for block in blocks)
    return SimpleNamespace(
        content=list(blocks), stop_reason="tool_use" if has_tool else "end_turn")


def test_case_skill_and_mcp_dynamic_tool_are_live_in_full_policy(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills" / "case-helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: case-helper\ndescription: case-only helper\n---\nUSE CASE DATA",
        encoding="utf-8",
    )
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(mcp, "TOOL_POLICY", run_eval.DOCKER_EVAL_TOOL_POLICY)
    mcp.mcp_clients.clear()
    try:
        assert "case-only helper" in skills.list_skills()
        assert "USE CASE DATA" in skills.load_skill("case-helper")
        assert "Discovered 2 tools" in mcp.connect_mcp("docs")
        tools, handlers = mcp.assemble_tool_pool()
        names = {tool["name"] for tool in tools}
        assert "mcp__docs__search" in names
        assert handlers["mcp__docs__search"](query="broker") == (
            "[docs] Found 3 results for 'broker'")
        monkeypatch.setattr(prompts, "TOOL_POLICY", run_eval.DOCKER_EVAL_TOOL_POLICY)
        prompt = prompts.assemble_system_prompt({
            "memories": "CASE MEMORY", "connected_mcp": ["docs"],
            "active_teammates": [],
        })
        assert "mcp__docs__search" in prompt
        assert "Search documentation" in prompt
        assert "CASE MEMORY" in prompt
    finally:
        mcp.mcp_clients.clear()


def test_acceptance_checklist_survives_as_compact_live_prompt_state(monkeypatch):
    contract = "Every failed reservation leaves inventory unchanged"
    basic_tools.CURRENT_TODOS.clear()
    try:
        assert basic_tools.run_todo_write([{
            "content": "Implement rollback",
            "status": "in_progress",
            "kind": "plan",
        }, {
            "content": contract,
            "status": "pending",
            "kind": "acceptance",
        }]).startswith("Updated 2 todos")

        live_context = context.update_context({}, [])
        monkeypatch.setattr(
            prompts, "TOOL_POLICY", run_eval.DOCKER_EVAL_TOOL_POLICY)
        prompt = prompts.assemble_system_prompt(live_context)
    finally:
        basic_tools.CURRENT_TODOS.clear()

    assert live_context["acceptance_todos"] == [{
        "id": "accept:1",
        "content": contract,
        "status": "pending",
        "kind": "acceptance",
    }]
    assert "Protected acceptance checklist" in prompt
    assert f"[accept:1 pending] {contract}" in prompt
    assert "Implement rollback" not in prompt


def test_persistent_task_protocol_and_worktree_use_case_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    tasks = state / ".tasks"
    worktrees = state / ".worktrees"
    mailboxes = state / ".mailboxes"
    for path in (tasks, worktrees, mailboxes):
        path.mkdir(parents=True)
    monkeypatch.setattr(task_system, "TASKS_DIR", tasks)
    monkeypatch.setattr(worktree_system, "WORKDIR", tmp_path)
    monkeypatch.setattr(worktree_system, "WORKTREES_DIR", worktrees)
    monkeypatch.setattr(protocol, "MAILBOX_DIR", mailboxes)

    task = task_system.create_task("full harness")
    assert task_system.claim_task(task.id) == f"Claimed {task.id} (full harness)"
    assert task_system.complete_task(task.id) == f"Completed {task.id} (full harness)"
    assert task_system.load_task(task.id).status == "completed"

    calls = []

    def fake_git(args):
        calls.append(args)
        if args[:2] == ["worktree", "add"]:
            Path(args[2]).mkdir(parents=True)
        return True, "ok"

    monkeypatch.setattr(worktree_system, "run_git", fake_git)
    assert "created" in worktree_system.create_worktree("case-wt").lower()
    assert calls[0][0:2] == ["worktree", "add"]
    assert Path(calls[0][2]).is_relative_to(worktrees)

    request = protocol.run_request_shutdown("worker")
    assert request == "Shutdown request sent to worker"
    pending = next(iter(protocol.pending_requests.values()))
    assert pending.target == "worker" and pending.status == "pending"


def test_subagent_and_teammate_have_bounded_full_runtime_lifecycle(monkeypatch):
    class TextClient:
        def __init__(self):
            self.messages = self

        def create(self, **_kwargs):
            return response(text_block("sub-runtime-done"))

    client = TextClient()
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    delegation = json.loads(subagent.spawn_subagent("finish"))
    assert delegation["role"] == "general"
    assert delegation["result"]["summary"] == "sub-runtime-done"
    assert delegation["routed_from"] == "task"

    monkeypatch.setattr(teammate, "client", client)
    monkeypatch.setattr(teammate, "MODEL", "scripted")
    assert "spawned" in teammate.spawn_teammate_thread(
        "worker", "tester", "finish").lower()
    assert teammate.stop_all_teammates(1.0) is True
    assert not teammate.teammate_threads


def test_background_completion_produces_notification():
    block = tool_block("bash", {"command": "test", "run_in_background": True}, "bg-call")
    background.background_tasks.clear()
    background.background_results.clear()

    def handler(**_kwargs):
        time.sleep(0.02)
        return "background complete"

    background.start_background_task(block, {"bash": handler})
    assert background.wait_for_background_tasks(1.0) is True
    notes = background.collect_background_results()
    assert len(notes) == 1
    assert "<task_notification>" in notes[0]
    assert "background complete" in notes[0]


def test_noninteractive_once_scheduler_reenters_agent_loop(tmp_path):
    class OnceClient:
        def __init__(self):
            self.messages = self
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return response(tool_block(
                    "schedule_once",
                    {"prompt": "once fired", "delay_seconds": 0.05,
                     "durable": False},
                    "once-call",
                ))
            return response(text_block("once handled"))

    client = OnceClient()
    state = tmp_path / "state"
    state.mkdir()
    result = agent_loop.run_agent_task(
        "schedule a one-time callback",
        str(tmp_path / "workspace"),
        model_client=client,
        model_provider="scripted",
        model="scripted",
        command_executor=LocalCommandExecutor(),
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
        runtime_root=str(state),
        manage_lifecycle=True,
    )

    assert result["final_answer"] == "once handled"
    assert len(client.calls) >= 2
    assert any(
        message.get("content") == "[Scheduled Once] once fired"
        for call in client.calls[1:]
        for message in call["messages"])
