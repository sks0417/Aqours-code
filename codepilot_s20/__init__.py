"""Engineering split of the learn-claude-code s20 comprehensive harness."""
__version__ = "0.1.0"
_BOOTSTRAPPED = False
def bootstrap():
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED: return
    from . import runtime_context, trace, task_system, worktree_system, skills, prompts, basic_tools, message_bus
    from . import protocol, autonomous, teammate, hooks, subagent, compact, recovery
    from . import background, cron, mcp, tool_handlers, tool_defs, context, agent_loop
    from . import runtime_state
    runtime_state.wire_modules()
    _BOOTSTRAPPED = True
bootstrap()
