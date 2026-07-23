from .runtime_state import *
from .runtime import AgentRuntime

# ── Prompt Assembly ──

ALL_TOOL_NAMES = [
    "bash", "read_file", "write_file", "edit_file", "glob", "todo_write",
    "task", "delegate_agent", "load_skill", "compact", "create_task", "list_tasks", "get_task",
    "claim_task", "complete_task", "schedule_cron", "schedule_once", "list_crons",
    "cancel_cron", "spawn_teammate", "send_message", "check_inbox",
    "request_shutdown", "request_plan", "review_plan", "create_worktree",
    "remove_worktree", "keep_worktree", "integrate_worktree", "connect_mcp",
]


PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "scheduling": ("Scheduling rules: use schedule_cron only for repeating "
                   "standard 5-field cron tasks (minute hour day-of-month "
                   "month day-of-week), such as daily, weekly, monthly, "
                   "hourly, or weekdays. schedule_cron has no seconds field "
                   "and must not be used for one-time tasks. Use "
                   "schedule_once for 'in N seconds', 'in N minutes', "
                   "'in N hours', tomorrow at a specific time, a concrete "
                   "date/time, or anything that should fire only once."),
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
    "permissions": ("If a tool result starts with 'Permission denied', stop immediately. "
                    "Do not suggest manual deletion, bypasses, alternative destructive methods, "
                    "or clearing files."),
    "tool_strategy": ("Tool strategy: prefer purpose-built read-only tools such as "
                      "glob and read_file over bash for inspection tasks. Do not "
                      "create temporary files just to count, sort, or inspect data. "
                      "For multi-step tasks with several file or tool operations, "
                      "inspect the task and relevant README/source first when needed, "
                      "then call todo_write before the first file change and keep the "
                      "todo list updated as work progresses. For complex code changes "
                      "grounded in a task, README, contract, or test suite, include "
                      "short, concrete kind=acceptance items for externally observable "
                      "requirements before editing. Plan items are implementation work "
                      "still to perform; acceptance items are contract outcomes, so do "
                      "not mark the only plan completed while edits/tests remain. Cover "
                      "every task-relevant explicit contract clause, including negative "
                      "error paths and normalized fields, not only failures visible in "
                      "public tests. After editing starts, retain those "
                      "requirements and add newly discovered ones instead of replacing "
                      "them. "
                      "Mark an acceptance item completed only after reviewing the "
                      "final change and attach concise evidence. Public tests alone "
                      "do not prove uncovered contract requirements. "
                      "If a tool result says 'Tool not run' with guidance, treat it "
                      "as a recoverable policy rejection: follow the guidance and "
                      "continue with a safer read-only approach. If a result starts "
                      "with 'Permission denied', stop immediately. After a Bash "
                      "command starts in the background, do not rerun that command, "
                      "use check_inbox to poll it, or launch a task/subagent solely "
                      "to wait. Continue independent work or finish the turn; its "
                      "task_notification will report the result."),
    "multiagent": ("Multiagent roles: the lead owns task decomposition, integration, "
                    "tests, and final claims. Use at most one explorer for fresh read-only "
                   "contract/code mapping. For complex changes, the harness may attach "
                   "one independent pre-final reviewer result automatically; do not call "
                   "a duplicate reviewer for that revision. Resolve every structured "
                   "finding or reject it with concrete code evidence even when the outer "
                   "verdict is incomplete. Do not repeat role "
                   "repository reads in the lead context by default. Use worker only for "
                   "one bounded implementation slice. delegate_agent(worker) automatically "
                   "owns Task and Worktree creation, so do not call create_task or "
                   "create_worktree first. A worker commit reaches the main workspace only "
                   "after integrate_worktree. Avoid delegating trivial work or sending the "
                    "whole task to a worker. When the finalization budget notice appears, "
                    "do not start any new role; use retained evidence for direct fixes, "
                    "targeted verification, and final. The compatibility task tool uses "
                    "the same bounded role runtime and automatically routes by delegated "
                    "intent; it is not an unrestricted second implementation path."),
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
    allowed_tools = (policy["allowed_tools"]
                     if "allowed_tools" in policy else ALL_TOOL_NAMES)
    try:
        live_tools, _ = assemble_tool_pool(runtime)
    except (NameError, AttributeError):
        live_tools = [tool for tool in BUILTIN_TOOLS
                      if tool.get("name") in allowed_tools]
    descriptions = {tool["name"]: tool.get("description", "")
                    for tool in live_tools}
    displayed_tools = list(allowed_tools)
    displayed_tools.extend(
        name for name in descriptions
        if name.startswith("mcp__") and name not in displayed_tools)
    tool_section = "Available tools (full descriptions):\n" + "\n".join(
        f"- {name}: {descriptions.get(name, '')}" for name in displayed_tools)
    if policy.get("allow_mcp", True):
        tool_section += " MCP tools are prefixed mcp__{server}__{tool}."
    workdir = runtime.paths.workdir if runtime is not None else WORKDIR
    prompt_runtime = resolve_prompt_runtime_context(policy, workdir)
    sections = [PROMPT_SECTIONS["identity"],
                tool_section,
                f"Working directory: {prompt_runtime['workdir']}",
                format_runtime_context_for_prompt(prompt_runtime),
                PROMPT_SECTIONS["tool_strategy"],
                PROMPT_SECTIONS["permissions"]]
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
