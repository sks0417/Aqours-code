from .runtime_state import *

# ── Context ──

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


def update_context(context: dict, messages: list) -> dict:
    memories = ""
    policy = TOOL_POLICY if isinstance(TOOL_POLICY, dict) else {}
    allow_memory = policy.get("allow_memory_context", True)
    allow_mcp = policy.get("allow_mcp", True)
    allow_teammates = policy.get("allow_teammate_context", True)
    if allow_memory and MEMORY_INDEX.exists():
        memories = MEMORY_INDEX.read_text()[:2000]
    return {
        "memories": memories,
        "connected_mcp": list(mcp_clients.keys()) if allow_mcp else [],
        "active_teammates": list(active_teammates.keys()) if allow_teammates else [],
    }



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
