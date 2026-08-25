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
    "permissions": (
        "Treat an ordinary 'Tool not run' policy block as recoverable: use the "
        "reason to rewrite the call or finish. If a tool result starts with "
        "'Permission denied', the user or runtime denied approval; stop "
        "immediately. Do not suggest manual deletion, bypasses, alternative "
        "destructive methods, or clearing files."
    ),
    "tool_strategy": (
        "Inspect via glob/read_file; avoid bash/temp files. For multi-step work, inspect "
        "contracts/source and maintain one todo_write implementation/verification "
        "checklist if useful. Batch independent calls in one response; sequence "
        "dependencies; max 8/batch. "
        "Once the exact intended changes for multiple independent files are known, "
        "emit their write_file or edit_file calls in the same response. Do not spend "
        "one model round per independent file. Prefer minimal edits that preserve "
        "existing correct behavior. Do not rewrite an entire existing file merely to "
        "batch mutations or simplify editing. Batch only independent, already-determined "
        "patches. Preserve unrelated public API, validation, error-handling, and "
        "security behavior. Use write_file for a new file or when the task genuinely "
        "requires replacing the complete file and its full behavior is already "
        "understood. For an existing file, prefer targeted edit_file changes when the "
        "unaffected structure and behavior can be preserved. Do not reread already-known "
        "files just for this rule. Sequence same-file/result-dependent mutations; max "
        "one/file/batch; never batch with tests/background/worktree integration. Calls "
        "run in order; partial "
        "failures keep successes. Complete a phase/round; skip announcements and short "
        "waits. "
        "After final verification succeeds and the workspace has not changed since "
        "that verification, do not reread unchanged source or test files merely to "
        "prepare the final answer or reconfirm completed work. Finish using the "
        "retained diff, checkpoint, and test evidence. Use tool evidence. Code "
        "changes permit reread/reverification. Before final verification, one concrete "
        "unresolved risk permits one focused check despite passing public tests. "
        "Otherwise avoid equivalent test reruns and broad audits. Background bash: do "
        "not rerun/poll/delegate to wait; work or finish via task_notification."
    ),
    "multiagent": (
        "task and delegate_agent run bounded temporary explore, plan, review, or "
        "general-purpose agents; they do not join shared Tasks, own Worktrees, or "
        "persist. spawn_teammate creates a persistent collaborator with shared "
        "Tasks and mailboxes that lives until Lead shutdown. Lead owns lifecycle, "
        "integration, tests, and final claims. Delegation is optional; use it only "
        "when useful and never after a finalization-budget notice."
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
    root_task = str(context.get("root_task", "")).strip()
    if root_task:
        sections.append(
            "Original task and hard constraints (authoritative):\n"
            + root_task[:6000]
        )
    sections.extend([
                PROMPT_SECTIONS["tool_strategy"],
                PROMPT_SECTIONS["permissions"],
    ])
    if any(name in allowed_tools for name in (
        "task", "delegate_agent", "spawn_teammate",
    )):
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
    todos = context.get("todos", [])
    if todos:
        lines = []
        for item in todos:
            status = item.get("status", "pending")
            item_id = item.get("id", "todo")
            line = f"- [{item_id} {status}] {item.get('content', '')}"
            lines.append(line)
        sections.append(
            "Active todo checklist:\n"
            + "\n".join(lines)
            + ("\nThe next todo_write replaces this list: resubmit every item "
               "to retain. Existing content may be omitted when its id is used."))
    return "\n\n".join(sections)



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
