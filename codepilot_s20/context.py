from .runtime_state import *
from .runtime import AgentRuntime

# ── Context ──

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


def update_context(
    context: dict,
    messages: list,
    runtime: AgentRuntime | None = None,
) -> dict:
    memories = ""
    runtime_policy = runtime.config.tool_policy if runtime is not None else None
    policy = (runtime_policy if isinstance(runtime_policy, dict)
              else TOOL_POLICY if isinstance(TOOL_POLICY, dict) else {})
    allow_memory = policy.get("allow_memory_context", True)
    allow_mcp = policy.get("allow_mcp", True)
    allow_teammates = policy.get("allow_teammate_context", True)
    memory_index = runtime.paths.memory_index if runtime is not None else MEMORY_INDEX
    if allow_memory and memory_index.exists():
        memories = memory_index.read_text()[:2000]
    todos = runtime.state.todos if runtime is not None else CURRENT_TODOS
    return {
        "memories": memories,
        "connected_mcp": list(mcp_clients.keys()) if allow_mcp else [],
        "active_teammates": list(active_teammates.keys()) if allow_teammates else [],
        # Keep only the compact acceptance contract in live prompt context.
        # Plan mechanics stay in ordinary tool history and can be compacted.
        "acceptance_todos": [
            dict(todo) for todo in todos
            if todo.get("kind") == "acceptance"
        ],
    }



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
