from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class AgentProfile:
    name: str
    description: str
    instructions: str
    tool_names: tuple[str, ...]
    max_rounds: int
    read_only: bool = True
    uses_worktree: bool = False


AGENT_PROFILES = {
    "explorer": AgentProfile(
        name="explorer",
        description=(
            "Read-only repository explorer that maps contracts, files, symbols, "
            "and producer-to-consumer relationships before implementation."
        ),
        instructions=(
            "Stay read-only. Inspect the task, repository guidance, and the "
            "smallest relevant source/test set. Trace real execution paths and "
            "derived values from producer through consumer. Distinguish verified "
            "facts from assumptions. Return JSON only with keys verdict, summary, "
            "requirements, code_map, risks, files_checked. verdict must be "
            "complete or blocked. Do not propose broad rewrites and do not edit."
        ),
        tool_names=("glob", "read_file"),
        max_rounds=5,
    ),
    "reviewer": AgentProfile(
        name="reviewer",
        description=(
            "Independent read-only correctness reviewer for final changes and "
            "uncovered contract requirements."
        ),
        instructions=(
            "Review independently from the implementing agent. Re-read the task "
            "and relevant contract, inspect changed files plus only direct "
            "dependencies needed to verify them, and look for behavior regressions, "
            "missing fields, failure branches, atomicity, idempotency, state "
            "transitions, and API compatibility when relevant. Return JSON only "
            "with keys verdict, summary, findings, files_checked, missing_evidence. "
            "verdict must be pass, gaps, or blocked. A pass requires concrete code "
            "evidence, not only public test success. Do not edit files."
        ),
        tool_names=("glob", "read_file"),
        max_rounds=5,
    ),
    "worker": AgentProfile(
        name="worker",
        description=(
            "Implementation agent for one bounded change in an isolated Git "
            "worktree."
        ),
        instructions=(
            "Implement only the delegated slice in the assigned worktree. Inspect "
            "the relevant contract and source before editing, make focused changes, "
            "and run targeted tests when useful. Do not spawn more agents, merge "
            "branches, or edit outside the delegated scope. The harness commits "
            "worktree changes after you finish. Return JSON only with keys verdict, "
            "summary, changed_files, tests, remaining_risks. verdict must be "
            "changes_ready, no_changes, or blocked."
        ),
        tool_names=("glob", "read_file", "write_file", "edit_file", "bash"),
        max_rounds=10,
        read_only=False,
        uses_worktree=True,
    ),
}


def get_agent_profile(role: str) -> AgentProfile | None:
    return AGENT_PROFILES.get(str(role or "").strip().lower())


def agent_profile_catalog() -> str:
    return "\n".join(
        f"- {profile.name}: {profile.description}"
        for profile in AGENT_PROFILES.values()
    )


def assess_task_complexity(text: str) -> dict:
    """Return a deterministic, provider-independent delegation hint.

    This is intentionally conservative. It does not attempt to understand the
    repository before tools run; the lead loop can escalate later when a task
    touches a broad file working set.
    """
    normalized = str(text or "").lower()
    score = 0
    reasons = []
    implementation_markers = (
        "implement", "fix", "build", "change", "modify", "refactor", "repair",
        "add feature", "create", "实现", "修复", "开发", "修改", "重构", "新增",
        "创建",
    )
    implementation_task = any(
        marker in normalized for marker in implementation_markers)

    if len(normalized) >= 320:
        score += 1
        reasons.append("long_task")

    contract_markers = (
        "contract", "requirement", "readme", "public api", "preserve",
        "compatibility", "documented", "acceptance", "契约", "要求", "接口",
        "兼容", "保持", "验收",
    )
    contract_hits = sum(1 for marker in contract_markers if marker in normalized)
    if contract_hits >= 2:
        score += 2
        reasons.append("multi_clause_contract")

    risk_markers = (
        "atomic", "idempot", "concurr", "transaction", "rollback", "state",
        "security", "permission", "race", "consistency", "migration",
        "exception", "error path", "原子", "幂等", "并发", "回滚", "状态",
        "安全", "一致", "异常",
    )
    risk_hits = sum(1 for marker in risk_markers if marker in normalized)
    if risk_hits >= 2:
        score += 2
        reasons.append("cross_cutting_risk")
    elif risk_hits == 1:
        score += 1
        reasons.append("behavioral_risk")

    verification_markers = (
        "test suite", "tests", "grader", "regression", "verify", "benchmark",
        "测试", "回归", "验证",
    )
    if any(marker in normalized for marker in verification_markers):
        score += 1
        reasons.append("verification_required")

    multi_scope_markers = (
        "multi-file", "multiple files", "service and", "repository", "api and",
        "end-to-end", "across", "多文件", "端到端", "跨文件", "仓库",
    )
    if any(marker in normalized for marker in multi_scope_markers):
        score += 1
        reasons.append("multi_component_scope")

    numbered_requirements = len(re.findall(r"(?:^|\n)\s*(?:[-*]|\d+[.)])\s+", normalized))
    if numbered_requirements >= 4:
        score += 1
        reasons.append("many_explicit_requirements")

    if score >= 5:
        level = "complex"
    elif score >= 3:
        level = "moderate"
    else:
        level = "simple"
    return {
        "level": level, "score": score, "reasons": reasons,
        "implementation_task": implementation_task,
    }


def complex_delegation_briefing(assessment: dict) -> str:
    reasons = ", ".join(assessment.get("reasons", [])) or "broad task scope"
    return (
        "<multiagent_policy level=\"complex\">This task was classified as "
        f"complex ({reasons}). Before the first implementation mutation, use "
        "delegate_agent(role=\"explorer\") to obtain an independent contract and "
        "code-path map, then rely on that result instead of repeating its broad "
        "reads. Delegate a worker only for a bounded implementation slice; workers "
        "write in isolated worktrees and their changes do not reach the main "
        "workspace until integrate_worktree is called. Before final, use a fresh "
        "delegate_agent(role=\"reviewer\") after the latest main-workspace change. "
        "A reviewer pass is required and becomes stale after another mutation. The "
        "lead owns decomposition, integration, tests, and the final answer."
        "</multiagent_policy>"
    )
