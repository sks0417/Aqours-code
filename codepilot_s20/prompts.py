from .runtime_state import *
from .runtime import AgentRuntime

# ── Prompt Assembly ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "scheduling": ("Scheduling: schedule_cron is only for repeating standard "
                   "5-field cron jobs and has no seconds field. Use "
                   "schedule_once for delays, concrete date-times, and every "
                   "one-time task."),
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
    "permissions": ("If a tool result starts with 'Permission denied', stop immediately. "
                    "Do not suggest manual deletion, bypasses, alternative destructive methods, "
                    "or clearing files."),
    "tool_strategy": (
        "Tool strategy: prefer glob/read_file to bash for inspection; do not "
        "create temporary inspection files. For multi-step work, inspect the "
        "contract/source, then call todo_write before editing and keep it current. "
        "Record every observable contract clause, including negative paths, as a "
        "kind=acceptance item; preserve discovered items and complete them only "
        "after final review with concise evidence. Public tests do not prove "
        "uncovered clauses. Treat 'Tool not run' as recoverable guidance. Stop on "
        "'Permission denied'. For background bash, never rerun, poll check_inbox, "
        "or delegate merely to wait; continue independent work or finish and use "
        "the task_notification result."
    ),
    "multiagent": (
        "Multiagent: the lead owns decomposition, integration, tests, and final "
        "claims. Use at most one explorer for fresh read-only mapping and one "
        "bounded worker slice. delegate_agent(worker) creates its Task and "
        "Worktree; integrate_worktree is required for its commit. The harness may "
        "attach one pre-final reviewer: do not duplicate it, and resolve each "
        "finding with code evidence. Avoid repeating role reads or delegating "
        "trivial/whole tasks. After a finalization-budget notice, start no new "
        "role. The task compatibility tool uses the same bounded role runtime."
    ),
}


def assemble_system_prompt(
    context: dict,
    runtime: AgentRuntime | None = None,
) -> str:
    # The system prompt is rebuilt each turn from live context. This is where
    # memory, skill catalog, MCP state, and active teammates become visible.
    runtime_policy = runtime.config.tool_policy if runtime is not None else None
    policy = (runtime_policy if isinstance(runtime_policy, dict)
              else TOOL_POLICY if isinstance(TOOL_POLICY, dict) else {})
    default_tools = list(TOOL_REGISTRY.names_for_role("lead"))
    allowed_tools = (policy["allowed_tools"]
                     if "allowed_tools" in policy else default_tools)
    tool_section = (
        "The API tool definitions and input schemas supplied with this request "
        "are authoritative. Use only tools present in that API tool list."
    )
    if policy.get("allow_mcp", True):
        tool_section += " Discovered MCP tools use mcp__{server}__{tool} names."
    workdir = runtime.paths.workdir if runtime is not None else WORKDIR
    prompt_runtime = resolve_prompt_runtime_context(policy, workdir)
    sections = [PROMPT_SECTIONS["identity"],
                tool_section,
                f"Working directory: {prompt_runtime['workdir']}",
                format_runtime_context_for_prompt(prompt_runtime)]
    working_memory = str(context.get("working_memory_prompt", "")).strip()
    if working_memory:
        sections.append(working_memory)
    sections.extend([
                PROMPT_SECTIONS["tool_strategy"],
                PROMPT_SECTIONS["permissions"],
    ])
    if "delegate_agent" in allowed_tools:
        sections.insert(-1, PROMPT_SECTIONS["multiagent"])
    if any(name in allowed_tools for name in ("schedule_cron", "schedule_once")):
        sections.insert(2, PROMPT_SECTIONS["scheduling"])
    sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
    if policy.get("allow_skill_context", True) and "load_skill" in allowed_tools:
        sections.append("Skills catalog:\n" + list_skills(runtime) +
                        "\nUse load_skill(name) when a skill is relevant.")
    if policy.get("allow_memory_context", True):
        sections.append(
            "Memory context:\n" + (context.get("memories") or "(no case memory)"))
    mcp_names = context.get("connected_mcp", []) if policy.get("allow_mcp", True) else []
    if policy.get("allow_mcp", True):
        sections.append(
            "MCP state:\nConnected servers: "
            + (", ".join(mcp_names) if mcp_names else "(none)"))
    if policy.get("allow_teammate_context", True):
        teammates = context.get("active_teammates", [])
        sections.append(
            "Active teammate state:\n"
            + (", ".join(teammates) if teammates else "(none)"))
    acceptance = context.get("acceptance_todos", [])
    if acceptance:
        lines = []
        for item in acceptance:
            status = item.get("status", "pending")
            item_id = item.get("id", "acceptance")
            line = f"- [{item_id} {status}] {item.get('content', '')}"
            if item.get("evidence"):
                line += f" | evidence: {item['evidence']}"
            lines.append(line)
        sections.append(
            "Protected acceptance checklist (verify before final):\n"
            + "\n".join(lines)
            + "\nUpdate an existing item by id; its original content need not be repeated.")
    return "\n\n".join(sections)



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
