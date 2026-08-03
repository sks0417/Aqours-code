"""Aqours_code local coding-agent harness."""

__version__ = "0.1.0"
_BOOTSTRAPPED = False


def bootstrap():
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    from . import (
        agent_loop,
        autonomous,
        background,
        basic_tools,
        compact,
        context,
        cron,
        hooks,
        mcp,
        message_bus,
        prompts,
        protocol,
        recovery,
        runtime_context,
        skills,
        subagent,
        task_system,
        teammate,
        tool_defs,
        tool_handlers,
        trace,
        worktree_system,
    )
    from . import runtime_state

    runtime_state.wire_modules()
    _BOOTSTRAPPED = True
