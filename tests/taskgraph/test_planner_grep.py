"""grep is offered to the planner and the team lead only; every other tool list is unchanged."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from aqours_code import bootstrap

bootstrap()

from aqours_code import mcp  # noqa: E402
from aqours_code.command_executor import LocalCommandExecutor  # noqa: E402
from aqours_code.runtime import AgentRuntime  # noqa: E402
from aqours_code.taskgraph import planner  # noqa: E402
from aqours_code.taskgraph.escapes import escape_reasons, is_refusal  # noqa: E402
from aqours_code.taskgraph.outline_levels import two_level_instructions  # noqa: E402
from aqours_code.taskgraph.planner_entry import (  # noqa: E402
    DRAFT_TOOL_POLICY,
    PLANNER_TOOL_POLICY,
    run_planner,
)
from aqours_code.taskgraph.planner_v3 import FACTS_PROMPT_PATH, load_facts_prompt  # noqa: E402
from aqours_code.taskgraph.team import LEAD_PROMPT  # noqa: E402
from aqours_code.taskgraph.worker_entry import WORKER_TOOL_POLICY, run_worker  # noqa: E402
from aqours_code.taskgraph.workers import AqoursWorker, WorkerRequest  # noqa: E402
from aqours_code.tool_defs import BUILTIN_TOOLS, TOOL_REGISTRY  # noqa: E402
from aqours_code.tool_registry import effective_tool_names  # noqa: E402
from test_planner import FakeClient, final  # noqa: E402

# The tool lists as they were before grep existed (feature/taskgraph-split-signals).
SINGLE_AGENT_TOOLS = [
    "bash", "read_file", "write_file", "edit_file", "glob", "todo_write", "task",
    "delegate_agent", "load_skill", "compact", "create_task", "list_tasks", "get_task",
    "claim_task", "complete_task", "schedule_cron", "schedule_once", "list_crons",
    "cancel_cron", "spawn_teammate", "send_message", "check_inbox", "request_shutdown",
    "request_plan", "review_plan", "create_worktree", "remove_worktree", "keep_worktree",
    "integrate_worktree", "connect_mcp",
]
# graph nodes, the taskgraph single control, team teammates and the lead's wrap-up
WORKER_TOOLS = ["bash", "read_file", "write_file", "edit_file", "glob", "todo_write",
                "compact"]
ROLE_TOOLS = {
    "lead": SINGLE_AGENT_TOOLS,
    "general-purpose": ["bash", "read_file", "write_file", "edit_file", "glob"],
    "explore": ["read_file"],
    "plan": ["read_file", "glob"],
    "review": ["read_file"],
    "teammate": ["bash", "read_file", "write_file", "edit_file", "glob", "list_tasks",
                 "claim_task", "complete_task", "send_message", "submit_plan"],
}
PLANNER_TOOLS_BEFORE = ["read_file", "glob", "compact"]
GREP_SENTENCE = "search for it with `grep` first,\nthen `read_file` only the few dozen lines"


def offered(policy, tmp_path) -> list[str]:
    runtime = AgentRuntime.create(
        workdir=tmp_path, model_client=SimpleNamespace(messages=object()),
        command_executor=LocalCommandExecutor(), model_provider="test", model="test",
        tool_policy=policy)
    return [tool["name"] for tool in mcp.assemble_tool_pool(runtime)[0]]


def agent_config(workspace, log_dir) -> dict:
    request = WorkerRequest(node_id="agent", attempt=1, prompt="Look.", timeout_s=60,
                            workspace=workspace, log_dir=log_dir)
    config = AqoursWorker().config_for(request)
    config.update(model_provider="scripted", model="scripted-model")
    return config


# ── the registry ──

def test_grep_is_an_opt_in_tool_of_the_lead_role():
    spec = TOOL_REGISTRY.get("grep")
    assert spec.opt_in is True
    assert spec.allowed_roles == frozenset({"lead"})
    assert spec.safety_policy == "standard" and spec.background_policy == "foreground"
    assert TOOL_REGISTRY.opt_in_names_for_role("lead") == ("grep",)
    assert set(spec.api_schema()["input_schema"]["properties"]) == {
        "pattern", "path", "glob", "ignore_case", "max_results"}


@pytest.mark.parametrize("role", sorted(ROLE_TOOLS))
def test_no_role_gets_grep_by_default(role):
    assert list(TOOL_REGISTRY.names_for_role(role)) == ROLE_TOOLS[role]
    assert "grep" not in {tool["name"] for tool in TOOL_REGISTRY.schemas_for_role(role)}


@pytest.mark.parametrize("role", ["explore", "plan", "review", "general-purpose", "teammate"])
def test_a_policy_cannot_give_grep_to_subagents_or_teammates(role):
    names = effective_tool_names(
        TOOL_REGISTRY, ["grep", "read_file"], role=role,
        parent_policy={"allowed_tools": ["grep", "read_file"]},
        delegated_policy={"allowed_tools": ["grep", "read_file"],
                          "allow_parent_permission_expansion": True})
    assert "grep" not in names


# ── tool lists of the agents ──

def test_single_agent_tools_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "TOOL_POLICY", None)
    assert offered(None, tmp_path) == SINGLE_AGENT_TOOLS
    assert [tool["name"] for tool in BUILTIN_TOOLS] == SINGLE_AGENT_TOOLS


def test_graph_nodes_and_teammates_are_offered_the_same_tools_as_before(tmp_path):
    assert WORKER_TOOL_POLICY["allowed_tools"] == WORKER_TOOLS
    assert offered(WORKER_TOOL_POLICY, tmp_path) == WORKER_TOOLS
    # what the model sees in a node attempt (the same worker runs teammates and wrap-up)
    client = FakeClient([final("done")])
    run_worker(agent_config(tmp_path, tmp_path / "logs"), model_client=client)
    assert client.tool_names[0] == WORKER_TOOLS


def test_a_policy_without_grep_never_gets_it(tmp_path):
    assert "grep" not in offered({"allowed_tools": ["read_file", "glob"]}, tmp_path)


def test_the_planner_and_the_team_lead_are_offered_grep(tmp_path):
    assert offered(PLANNER_TOOL_POLICY, tmp_path) == [*PLANNER_TOOLS_BEFORE, "grep"]
    client = FakeClient([final("done")])
    run_planner(agent_config(tmp_path, tmp_path / "logs"), model_client=client)
    assert client.tool_names[0] == [*PLANNER_TOOLS_BEFORE, "grep"]
    # the team lead plans through the same worker: the planner entry's ground stage
    assert planner.planner_worker().entry_command == [
        sys.executable, "-m", "aqours_code.taskgraph.planner_entry"]


def test_the_draft_stage_is_not_offered_grep(tmp_path):
    assert DRAFT_TOOL_POLICY["allowed_tools"] == ["compact"]
    client = FakeClient([final("done")])
    run_planner(agent_config(tmp_path, tmp_path / "logs"), model_client=client,
                stage="draft")
    assert client.tool_names[0] == ["compact"]


def test_the_planner_agent_runs_grep_in_its_workspace(toy_repo, tmp_path):
    from test_repo_guard import tool_results
    from test_planner import tool_use

    client = FakeClient([tool_use("grep", "g1", pattern=r"def run_loop", glob="*.py"),
                         final("done")])
    recorded = []
    inner_create = client.create

    def create(**kwargs):
        recorded.append(kwargs.get("messages") or [])
        return inner_create(**kwargs)

    client.create = create
    run_planner(agent_config(toy_repo.path, tmp_path / "logs"), model_client=client)
    assert tool_results(recorded[-1])["g1"] == "runner.py:6: def run_loop(store: JobStore) -> int:"


# ── prompts, version, audit ──

def test_the_facts_prompt_says_to_grep_first_in_both_outline_modes():
    text = FACTS_PROMPT_PATH.read_text(encoding="utf-8")
    assert GREP_SENTENCE in text
    assert "You have read-only tools (read_file, glob, grep)." in text
    assert GREP_SENTENCE in load_facts_prompt("two-level")
    assert two_level_instructions(text) != text  # the outline paragraph is still found


def test_the_lead_prompt_says_to_grep_first():
    text = LEAD_PROMPT.substitute(request="R", files="F", n=2)
    assert "search for it with grep first,\nthen read_file only the few dozen lines" in text


def test_planner_version_marks_the_grep_planner():
    assert planner.PLANNER_VERSION == "planner-v3.2"


def test_the_escape_audit_reads_grep_paths():
    assert escape_reasons("grep", {"pattern": "x", "path": "C:/Users/someone/x"})
    assert escape_reasons("grep", {"pattern": "x", "path": "pkg"}) == []
    assert is_refusal("grep", "Error: Path escapes workspace: ../")
