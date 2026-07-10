from __future__ import annotations
from .config import *
from .runtime_context import *
from .trace import *
from .terminal import PROMPT, terminal_print
_REGISTERED_MODULES = []
def register_module(module):
    if module not in _REGISTERED_MODULES:
        _REGISTERED_MODULES.append(module)
def export_public(namespace: dict):
    for name, value in namespace.items():
        if name.startswith("_") and name not in {"_normalize_todos", "_task_path", "_count_worktree_changes", "_teammate_submit_plan", "_parse_frontmatter"}:
            continue
        if name in {"_sys", "_runtime_state"}:
            continue
        globals()[name] = value
def wire_modules():
    shared = {name: value for name, value in globals().items() if not name.startswith("__")}
    for module in _REGISTERED_MODULES:
        module.__dict__.update(shared)
