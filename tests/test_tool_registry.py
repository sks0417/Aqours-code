from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codepilot_s20 import bootstrap

bootstrap()

from codepilot_s20 import background, hooks, mcp, prompts, subagent, teammate
from codepilot_s20.agent_profiles import get_agent_profile
from codepilot_s20.command_executor import LocalCommandExecutor
from codepilot_s20.runtime import AgentRuntime
from codepilot_s20.tool_defs import (
    BUILTIN_HANDLERS,
    BUILTIN_TOOLS,
    TOOL_REGISTRY,
    builtin_handlers,
    tool_schemas_for_names,
)
from codepilot_s20.tool_registry import (
    effective_tool_names,
)


def test_registry_is_the_authoritative_lead_surface():
    lead_names = TOOL_REGISTRY.names_for_role("lead")

    assert len(set(lead_names)) == len(lead_names)
    assert set(lead_names) <= {
        spec.name for spec in TOOL_REGISTRY.specs()
    }
    assert "read_archived_tool_result" not in lead_names
    assert "search_archived_tool_results" not in lead_names
    assert [tool["name"] for tool in BUILTIN_TOOLS] == list(lead_names)
    assert set(BUILTIN_HANDLERS) == set(lead_names) - {"compact"}
    assert builtin_handlers() == BUILTIN_HANDLERS

    for tool in BUILTIN_TOOLS:
        spec = TOOL_REGISTRY.get(tool["name"])
        assert tool == spec.api_schema()
        assert BUILTIN_HANDLERS.get(spec.name) is spec.handler
        assert spec.safety_policy
        assert spec.background_policy


def test_role_agents_and_teammates_project_the_same_specs():
    for role in ("general-purpose", "explore", "plan", "review"):
        profile = get_agent_profile(role)
        projected = tool_schemas_for_names(profile.tool_names, role=role)
        assert {tool["name"] for tool in projected} == set(profile.tool_names)
        for tool in projected:
            assert tool == TOOL_REGISTRY.get(tool["name"]).api_schema()

    teammate_names = set(TOOL_REGISTRY.names_for_role("teammate"))
    assert teammate_names == {
        "bash", "read_file", "write_file", "edit_file", "glob",
        "send_message", "submit_plan", "list_tasks", "claim_task",
        "complete_task",
    }
    assert "lead" not in TOOL_REGISTRY.get("submit_plan").allowed_roles
    delegate = TOOL_REGISTRY.get("delegate_agent").api_schema()
    assert delegate["input_schema"]["properties"]["role"]["enum"] == [
        "explore", "plan", "review", "general-purpose",
    ]
    assert "task_id" not in delegate["input_schema"]["properties"]


def test_teammate_focus_never_removes_team_protocol(tmp_path, monkeypatch):
    runtime = AgentRuntime.create(
        workdir=tmp_path,
        model_client=SimpleNamespace(messages=object()),
        command_executor=LocalCommandExecutor(),
        model_provider="test",
        model="test",
        tool_policy=None,
    )
    monkeypatch.setattr(teammate, "TOOL_POLICY", {})
    expected = set(TOOL_REGISTRY.names_for_role("teammate"))

    assert expected == {
        "bash", "read_file", "write_file", "edit_file", "glob",
        "send_message", "submit_plan", "list_tasks", "claim_task",
        "complete_task",
    }
    for focus in ("recovery", "tests", "researcher", "review"):
        assert set(teammate.effective_teammate_tool_names(
            focus, runtime,
        )) == expected


def test_registry_policy_metadata_drives_existing_policy_categories():
    assert TOOL_REGISTRY.get("bash").safety_policy == "command_guard"
    assert TOOL_REGISTRY.get("bash").background_policy == "slow_or_explicit"
    assert TOOL_REGISTRY.get("read_file").background_policy == "foreground"
    assert TOOL_REGISTRY.get("write_file").safety_policy == "workspace_write"
    assert TOOL_REGISTRY.get("edit_file").safety_policy == "workspace_write"


def test_parent_runtime_policy_caps_subagent_and_teammate_tools(
    tmp_path, monkeypatch,
):
    policy = {
        "allowed_tools": [
            "read_file",
            "delegate_agent",
            "spawn_teammate",
        ],
    }
    runtime = AgentRuntime.create(
        workdir=tmp_path,
        model_client=SimpleNamespace(messages=object()),
        command_executor=LocalCommandExecutor(),
        model_provider="test",
        model="test",
        tool_policy=policy,
    )
    monkeypatch.setattr(teammate, "TOOL_POLICY", {})
    helper = get_agent_profile("general-purpose")

    helper_tools = effective_tool_names(
        TOOL_REGISTRY,
        helper.tool_names,
        role="general-purpose",
        parent_policy=policy,
        environment_policy={},
    )
    environment_capped = effective_tool_names(
        TOOL_REGISTRY,
        helper.tool_names,
        role="general-purpose",
        parent_policy={"allowed_tools": list(helper.tool_names)},
        environment_policy={"allowed_tools": ["read_file"]},
    )
    explicit_expansion = effective_tool_names(
        TOOL_REGISTRY,
        helper.tool_names,
        role="general-purpose",
        parent_policy={"allowed_tools": ["read_file"]},
        environment_policy={"allowed_tools": ["read_file", "bash"]},
        delegated_policy={
            "allowed_tools": ["bash"],
            "allow_parent_permission_expansion": True,
        },
    )
    teammate_tools = teammate.effective_teammate_tool_names(
        "worker", runtime,
    )

    assert set(helper_tools) == {"read_file"}
    assert set(environment_capped) == {"read_file"}
    assert set(explicit_expansion) == {"bash"}
    assert set(teammate_tools) == {"read_file"}
    assert {"bash", "write_file", "edit_file"}.isdisjoint(helper_tools)
    assert {"bash", "write_file", "edit_file"}.isdisjoint(teammate_tools)

    captured = {}

    class CapturingClient:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            captured["tools"] = kwargs["tools"]
            return SimpleNamespace(content=[
                SimpleNamespace(
                    type="text",
                    text='{"verdict":"blocked","summary":"done"}',
                ),
            ])

    runtime.services.model_client = CapturingClient()
    monkeypatch.setattr(subagent, "TOOL_POLICY", {})
    subagent.run_role_agent("general-purpose", "inspect", tmp_path, runtime)

    assert {
        tool["name"] for tool in captured["tools"]
    } == {"read_file"}


def test_api_schema_and_role_projections_cannot_pollute_registry():
    canonical = TOOL_REGISTRY.get("bash")
    with pytest.raises(TypeError):
        canonical.schema["properties"]["command"]["type"] = "number"
    first = canonical.api_schema()
    first["input_schema"]["properties"]["command"]["type"] = "number"
    first["input_schema"]["properties"]["new_field"] = {"type": "string"}
    helper_projection = tool_schemas_for_names(
        ["bash"], role="general-purpose")
    helper_projection[0]["input_schema"]["required"].append("new_field")

    fresh = canonical.api_schema()
    lead_projection = TOOL_REGISTRY.schemas_for_role("lead")
    lead_bash = next(tool for tool in lead_projection if tool["name"] == "bash")

    assert fresh["input_schema"]["properties"]["command"]["type"] == "string"
    assert "new_field" not in fresh["input_schema"]["properties"]
    assert fresh["input_schema"]["required"] == ["command"]
    assert lead_bash == fresh


def test_every_declared_policy_has_an_execution_dispatcher(monkeypatch):
    safety = {spec.safety_policy for spec in TOOL_REGISTRY.specs()}
    background_policies = {
        spec.background_policy for spec in TOOL_REGISTRY.specs()
    }
    assert all(hooks.has_safety_policy_validator(item) for item in safety)
    assert all(
        background.has_background_policy_router(item)
        for item in background_policies
    )

    monkeypatch.setattr(hooks, "APPROVAL_MODE", "never")
    destructive = SimpleNamespace(
        name="remove_worktree",
        input={"name": "worker", "discard_changes": True},
    )
    invalid_integration = SimpleNamespace(
        name="integrate_worktree",
        input={"name": "../outside", "cleanup": True},
    )
    assert hooks.permission_hook(destructive)["recoverable"] is False
    assert "invalid Worktree name" in hooks.permission_hook(
        invalid_integration,
    )
    assert background.background_reason(
        "read_file", {"run_in_background": True},
    ) is None
    assert background.background_reason(
        "bash", {"command": "echo ok", "run_in_background": True},
    ) == "explicit"


def test_fixed_prompt_and_lead_tool_schema_stay_below_phase_two_budget(
    monkeypatch,
):
    mcp.mcp_clients.clear()
    monkeypatch.setattr(prompts, "list_skills", lambda _runtime=None: "(none)")
    monkeypatch.setattr(prompts, "WORKDIR", "/workspace")
    monkeypatch.setattr(prompts, "TOOL_POLICY", {})
    monkeypatch.setattr(mcp, "TOOL_POLICY", {})
    tools, _ = mcp.assemble_tool_pool()
    system = prompts.assemble_system_prompt({})
    fixed_payload = json.dumps(
        {"system": system, "tools": tools},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert {tool["name"] for tool in tools} == set(
        TOOL_REGISTRY.names_for_role("lead")
    )
    assert len(fixed_payload) < 12_000
    assert "API tool definitions and input schemas" in system
    assert "Available tools (full descriptions):" not in system
    assert TOOL_REGISTRY.get("delegate_agent").description not in system


def test_tool_strategy_batches_only_safe_independent_operations():
    strategy = prompts.PROMPT_SECTIONS["tool_strategy"]

    assert "Batch independent reads" in strategy
    assert "at most 8" in strategy
    assert "each targets a different file" in strategy
    assert "at most one mutation per file" in strategy
    assert "none depends on another result" in strategy
    assert "read-to-dependent-edit" in strategy
    assert "mutations with tests, background commands, or worktree" in strategy
    assert "Tools execute in response order" in strategy
    assert "partial failure does not roll back" in strategy
    assert "Complete one coherent phase" in strategy
