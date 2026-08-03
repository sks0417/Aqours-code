from .runtime_state import *
from .runtime import AgentRuntime

# ── Skill Loading ──

SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


def scan_skills(runtime: AgentRuntime | None = None):
    SKILL_REGISTRY.clear()
    skills_dir = runtime.paths.skills_dir if runtime is not None else SKILLS_DIR
    if not skills_dir.exists():
        return
    for directory in sorted(skills_dir.iterdir()):
        if not directory.is_dir():
            continue
        manifest = directory / "SKILL.md"
        if not manifest.exists():
            continue
        raw = manifest.read_text()
        meta, _ = _parse_frontmatter(raw)
        name = meta.get("name", directory.name)
        desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
        SKILL_REGISTRY[name] = {
            "name": name,
            "description": desc,
            "content": raw,
        }


def list_skills(runtime: AgentRuntime | None = None) -> str:
    # Scan lazily from the current runtime SKILLS_DIR. Restricted eval policies
    # never call this function, so host skill manifests are not read at import.
    scan_skills(runtime)
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- {skill['name']}: {skill['description']}"
        for skill in SKILL_REGISTRY.values())


def load_skill(name: str, runtime: AgentRuntime | None = None) -> str:
    scan_skills(runtime)
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        available = ", ".join(SKILL_REGISTRY.keys()) or "(none)"
        return f"Skill not found: {name}. Available: {available}"
    return skill["content"]



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
