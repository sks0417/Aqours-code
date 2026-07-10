from .runtime_state import *

# ── Prompt Assembly ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, edit_file, glob, "
             "todo_write, task, load_skill, compact, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "schedule_cron, schedule_once, list_crons, cancel_cron, "
             "spawn_teammate, send_message, check_inbox, "
             "request_shutdown, request_plan, review_plan, "
             "create_worktree, remove_worktree, keep_worktree, "
             "connect_mcp. MCP tools are prefixed mcp__{server}__{tool}.",
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
                      "call todo_write before the first non-todo tool call and keep "
                      "the todo list updated as work progresses. "
                      "If a tool result says 'Tool not run' with guidance, treat it "
                      "as a recoverable policy rejection: follow the guidance and "
                      "continue with a safer read-only approach. If a result starts "
                      "with 'Permission denied', stop immediately."),
}


def assemble_system_prompt(context: dict) -> str:
    # The system prompt is rebuilt each turn from live context. This is where
    # memory, skill catalog, MCP state, and active teammates become visible.
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["scheduling"],
                PROMPT_SECTIONS["workspace"],
                format_runtime_context_for_prompt(detect_runtime_context(WORKDIR)),
                PROMPT_SECTIONS["tool_strategy"],
                PROMPT_SECTIONS["permissions"]]
    sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
    sections.append("Skills catalog:\n" + list_skills() +
                    "\nUse load_skill(name) when a skill is relevant.")
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")
    mcp_names = list(mcp_clients.keys())
    if mcp_names:
        sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")
    return "\n\n".join(sections)



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
