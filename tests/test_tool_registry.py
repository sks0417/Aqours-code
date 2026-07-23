from __future__ import annotations

import json

from codepilot_s20 import bootstrap

bootstrap()

from codepilot_s20 import mcp, prompts
from codepilot_s20.agent_profiles import get_agent_profile
from codepilot_s20.tool_defs import (
    BUILTIN_HANDLERS,
    BUILTIN_TOOLS,
    TOOL_REGISTRY,
    builtin_handlers,
    tool_schemas_for_names,
)


def test_registry_is_the_authoritative_30_tool_lead_surface():
    lead_names = TOOL_REGISTRY.names_for_role("lead")

    assert len(TOOL_REGISTRY) == 31  # 30 lead tools + teammate-only submit_plan
    assert len(lead_names) == 30
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
    for role in ("general", "explorer", "reviewer", "worker"):
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


def test_registry_policy_metadata_drives_existing_policy_categories():
    assert TOOL_REGISTRY.get("bash").safety_policy == "command_guard"
    assert TOOL_REGISTRY.get("bash").background_policy == "slow_or_explicit"
    assert TOOL_REGISTRY.get("read_file").background_policy == "foreground"
    assert TOOL_REGISTRY.get("write_file").safety_policy == "workspace_write"
    assert TOOL_REGISTRY.get("edit_file").safety_policy == "workspace_write"


def test_fixed_prompt_and_30_tool_schema_stay_below_phase_two_budget():
    mcp.mcp_clients.clear()
    tools, _ = mcp.assemble_tool_pool()
    system = prompts.assemble_system_prompt({})
    fixed_payload = json.dumps(
        {"system": system, "tools": tools},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert len(tools) == 30
    assert len(fixed_payload) < 12_000
    assert "API tool definitions and input schemas" in system
    assert "Available tools (full descriptions):" not in system
    assert TOOL_REGISTRY.get("delegate_agent").description not in system

