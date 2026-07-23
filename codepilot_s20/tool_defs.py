from .runtime_state import *
from collections.abc import Callable
from .runtime import AgentRuntime
from .tool_registry import ToolRegistry, ToolSpec
from .basic_tools import (
    run_bash,
    run_edit,
    run_glob,
    run_read,
    run_todo_write,
    run_write,
)
from .cron import (
    run_cancel_cron,
    run_list_crons,
    run_schedule_cron,
    run_schedule_once,
)
from .mcp import connect_mcp as run_connect_mcp
from .protocol import (
    run_request_plan,
    run_request_shutdown,
    run_review_plan,
)
from .skills import load_skill
from .subagent import delegate_agent, spawn_subagent
from .tool_handlers import (
    run_check_inbox,
    run_claim_task,
    run_complete_task,
    run_create_task,
    run_create_worktree,
    run_get_task,
    run_integrate_worktree,
    run_keep_worktree,
    run_list_tasks,
    run_remove_worktree,
    run_send_message,
    run_spawn_teammate,
    run_delegate_agent,
)

# ── Tool Definitions ──

# These compact declarations are consumed once below to build ToolSpec objects.
# TOOL_REGISTRY is the only public/authoritative lookup surface.
_TOOL_SCHEMAS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"},
                                     "run_in_background": {"type": "boolean"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": ("Find files matching a glob pattern; ** "
                                      "is recursive. Prefer one task-relevant "
                                      "pattern and do not probe unrelated "
                                      "language extensions."),
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
    {"name": "todo_write",
     "description": ("Create and manage implementation plan steps and contract "
                     "acceptance criteria. Use kind=plan for work still to do and "
                     "kind=acceptance for every externally required outcome, "
                     "including error paths omitted by public tests. Completed "
                     "acceptance items require concise evidence. Existing "
                     "acceptance items have stable IDs; update one by sending "
                     "its id, status, and evidence without copying its content."),
     "input_schema": {"type": "object",
                      "properties": {"todos": {"type": "array", "maxItems": 20,
                          "items": {"type": "object",
                                    "properties": {
                                        "id": {"type": "string", "maxLength": 100},
                                        "content": {"type": "string", "maxLength": 500},
                                        "status": {"type": "string",
                                                   "enum": ["pending", "in_progress", "completed"]},
                                        "kind": {"type": "string",
                                                 "enum": ["plan", "acceptance"]},
                                        "evidence": {"type": "string", "maxLength": 500}},
                                    "required": ["status"]}}},
                      "required": ["todos"]}},
    {"name": "task",
     "description": ("Compatibility entry for one focused delegation. The harness "
                     "routes inspection to explorer, implementation to an isolated "
                     "worker worktree, final audit to reviewer, and a small unmatched "
                     "question to a read-only general helper. Returns a structured "
                     "envelope; worker changes require integrate_worktree. Do not use "
                     "it merely to wait for a background task_notification."),
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string"}},
                      "required": ["description"]}},
    {"name": "delegate_agent",
     "description": ("Delegate a bounded task to a fresh role context. general "
                     "answers one small read-only question; explorer "
                     "maps contracts/code read-only; reviewer independently audits "
                     "final correctness read-only; worker edits only an isolated "
                     "worktree and returns a commit that must be integrated. For "
                     "worker, this tool automatically creates and owns the Task and "
                     "Worktree; do not create them first."),
     "input_schema": {"type": "object",
                      "properties": {
                          "role": {"type": "string",
                                   "enum": ["general", "explorer", "reviewer", "worker"]},
                          "prompt": {"type": "string"},
                          "name": {"type": "string"},
                          "task_id": {"type": "string"}},
                      "required": ["role", "prompt"]}},
    {"name": "load_skill",
     "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "compact",
     "description": "Summarize earlier conversation and continue with compacted context.",
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string"}},
                      "required": []}},
    {"name": "create_task",
     "description": ("Low-level manual task API. Do not call this before "
                     "delegate_agent(role=worker), which creates its own task."),
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                     "description": {"type": "string"},
                                     "blockedBy": {"type": "array",
                                                   "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_task", "description": "Get full task details.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a pending task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete an in-progress task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "schedule_cron",
     "description": ("Schedule a repeating task with standard 5-field cron: "
                     "minute hour day-of-month month day-of-week. "
                     "Use only for recurring schedules such as daily, weekly, "
                     "monthly, hourly, or weekdays. It does not support "
                     "seconds and must not be used for 'in N seconds/minutes' "
                     "or one-time tasks. Examples: daily 9:30 is "
                     "'30 9 * * *'; weekdays 18:00 is '0 18 * * 1-5'."),
     "input_schema": {"type": "object",
                      "properties": {"cron": {"type": "string"},
                                     "prompt": {"type": "string"},
                                     "durable": {"type": "boolean"}},
                      "required": ["cron", "prompt"]}},
    {"name": "schedule_once",
     "description": ("Schedule a one-time task. Use this for seconds-level "
                     "delays, minute/hour delays, tomorrow at a specific time, "
                     "or any concrete date-time that should fire once and then "
                     "disappear. Provide either delay_seconds, e.g. 5 for "
                     "'5 seconds later' or 600 for '10 minutes later', or "
                     "run_at as local ISO time like '2026-07-06 16:30'. "
                     "Do not use schedule_cron for one-time or seconds-level "
                     "requests. If the app is not running at the due time, "
                     "expired one-time jobs are skipped on restart rather "
                     "than replayed."),
     "input_schema": {"type": "object",
                      "properties": {"prompt": {"type": "string"},
                                     "delay_seconds": {"type": "number"},
                                     "run_at": {"type": "string"},
                                     "durable": {"type": "boolean"}},
                      "required": ["prompt"]}},
    {"name": "list_crons", "description": "List registered cron jobs.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "cancel_cron", "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
    {"name": "spawn_teammate", "description": "Spawn an autonomous teammate.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "role": {"type": "string"},
                                     "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message", "description": "Send message to a teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check inbox for messages and protocol responses.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "request_shutdown",
     "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"}},
                      "required": ["teammate"]}},
    {"name": "request_plan",
     "description": "Ask a teammate to submit a plan.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},
    {"name": "review_plan",
     "description": "Approve or reject a submitted plan.",
     "input_schema": {"type": "object",
                      "properties": {"request_id": {"type": "string"},
                                     "approve": {"type": "boolean"},
                                     "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
    {"name": "create_worktree",
     "description": ("Low-level manual worktree API. Do not call this before "
                     "delegate_agent(role=worker), which creates its own worktree."),
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "task_id": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "remove_worktree",
     "description": "Remove a worktree. Refuses if changes exist.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "discard_changes": {"type": "boolean"}},
                      "required": ["name"]}},
    {"name": "keep_worktree",
     "description": "Keep a worktree for manual review.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "integrate_worktree",
     "description": ("Integrate a finalized worker worktree into the lead workspace. "
                     "Refuses overlapping lead/worker file changes and preserves "
                     "the worktree when integration cannot complete."),
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "cleanup": {"type": "boolean"}},
                      "required": ["name"]}},
    {"name": "connect_mcp",
     "description": "Connect to an MCP server (docs, deploy) and discover tools.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "submit_plan",
     "description": "Submit a plan for Lead approval.",
     "input_schema": {"type": "object",
                      "properties": {"plan": {"type": "string"}},
                      "required": ["plan"]}},
]

_TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
    "todo_write": run_todo_write, "task": spawn_subagent,
    "delegate_agent": run_delegate_agent,
    "load_skill": load_skill,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
    "schedule_cron": run_schedule_cron,
    "schedule_once": run_schedule_once,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message, "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan, "review_plan": run_review_plan,
    "create_worktree": run_create_worktree,
    "remove_worktree": run_remove_worktree,
    "keep_worktree": run_keep_worktree,
    "integrate_worktree": run_integrate_worktree,
    "connect_mcp": run_connect_mcp,
}

_LEAD = frozenset({"lead"})
_ROLE_ACCESS = {
    "bash": frozenset({"lead", "worker", "teammate"}),
    "read_file": frozenset({
        "lead", "general", "explorer", "reviewer", "worker", "teammate",
    }),
    "write_file": frozenset({"lead", "worker", "teammate"}),
    "edit_file": frozenset({"lead", "worker", "teammate"}),
    "glob": frozenset({"lead", "general", "worker", "teammate"}),
    "send_message": frozenset({"lead", "teammate"}),
    "list_tasks": frozenset({"lead", "teammate"}),
    "claim_task": frozenset({"lead", "teammate"}),
    "complete_task": frozenset({"lead", "teammate"}),
    "submit_plan": frozenset({"teammate"}),
}
_RUNTIME_AWARE = frozenset({
    "bash", "read_file", "write_file", "edit_file", "glob", "todo_write",
    "load_skill", "task", "delegate_agent", "integrate_worktree",
})
_SAFETY_POLICIES = {
    "bash": "command_guard",
    "write_file": "workspace_write",
    "edit_file": "workspace_write",
    "remove_worktree": "destructive_confirmation",
    "integrate_worktree": "workspace_integration",
}
_BACKGROUND_POLICIES = {"bash": "slow_or_explicit"}

TOOL_REGISTRY = ToolRegistry(
    ToolSpec(
        name=tool["name"],
        description=tool["description"],
        schema=tool["input_schema"],
        handler=_TOOL_HANDLERS.get(tool["name"]),
        safety_policy=_SAFETY_POLICIES.get(tool["name"], "standard"),
        background_policy=_BACKGROUND_POLICIES.get(
            tool["name"], "foreground",
        ),
        allowed_roles=_ROLE_ACCESS.get(tool["name"], _LEAD),
        runtime_aware=tool["name"] in _RUNTIME_AWARE,
    )
    for tool in _TOOL_SCHEMAS
)

# Compatibility exports are derived views, not independent definitions.
BUILTIN_TOOLS = TOOL_REGISTRY.schemas_for_role("lead")
BUILTIN_HANDLERS = TOOL_REGISTRY.handlers_for_role("lead")


def builtin_handlers(
    runtime: AgentRuntime | None = None,
) -> dict[str, Callable]:
    """Return handlers bound to one runtime where the tool supports it.

    The static table remains as a compatibility surface for older callers.
    Binding here lets the main Agent stop discovering workspace, deadline,
    executor, and Todo state through module globals.
    """
    return TOOL_REGISTRY.handlers_for_role("lead", runtime)


def tool_schemas_for_role(role: str) -> list[dict]:
    return TOOL_REGISTRY.schemas_for_role(role)


def tool_schemas_for_names(names, *, role: str) -> list[dict]:
    return TOOL_REGISTRY.schemas_for_names(names, role=role)


def tool_handlers_for_names(
    names,
    *,
    role: str,
    runtime: AgentRuntime | None = None,
    **handler_kwargs,
) -> dict[str, Callable]:
    return TOOL_REGISTRY.handlers_for_names(
        names, role=role, runtime=runtime, **handler_kwargs,
    )


def get_tool_spec(name: str) -> ToolSpec:
    return TOOL_REGISTRY.get(name)



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
