from .runtime_state import *
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

# The model sees tool schemas; Python executes handlers. S20 keeps both tables
# explicit so every added capability is visible in one place.
BUILTIN_TOOLS = [
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
                     "acceptance items require concise evidence."),
     "input_schema": {"type": "object",
                      "properties": {"todos": {"type": "array", "maxItems": 20,
                          "items": {"type": "object",
                                    "properties": {
                                        "content": {"type": "string", "maxLength": 500},
                                        "status": {"type": "string",
                                                   "enum": ["pending", "in_progress", "completed"]},
                                        "kind": {"type": "string",
                                                 "enum": ["plan", "acceptance"]},
                                        "evidence": {"type": "string", "maxLength": 500}},
                                    "required": ["content", "status"]}}},
                      "required": ["todos"]}},
    {"name": "task",
     "description": ("Launch a focused subagent for independent delegated work. "
                     "Returns only its final summary. Do not use it merely to wait "
                     "for a background task_notification."),
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string"}},
                      "required": ["description"]}},
    {"name": "delegate_agent",
     "description": ("Delegate a bounded task to a fresh role context. explorer "
                     "maps contracts/code read-only; reviewer independently audits "
                     "final correctness read-only; worker edits only an isolated "
                     "worktree and returns a commit that must be integrated."),
     "input_schema": {"type": "object",
                      "properties": {
                          "role": {"type": "string",
                                   "enum": ["explorer", "reviewer", "worker"]},
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
    {"name": "create_task", "description": "Create a task.",
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
     "description": "Create an isolated git worktree.",
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
]

BUILTIN_HANDLERS = {
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



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
