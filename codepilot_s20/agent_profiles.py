from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class AgentProfile:
    name: str
    description: str
    instructions: str
    tool_names: tuple[str, ...]
    max_tool_rounds: int
    max_read_paths: int
    max_tool_calls: int
    max_response_tokens: int
    read_only: bool = True


AGENT_PROFILES = {
    "general-purpose": AgentProfile(
        name="general-purpose",
        description=(
            "Bounded temporary coding agent for one focused task that may require "
            "inspection, dependent tool use, or direct workspace edits."
        ),
        instructions=(
            "Complete one focused delegated task in the assigned workspace. Inspect "
            "only the files needed, make direct edits only when the assignment asks "
            "for them, and run targeted checks when useful. Do not create or claim "
            "shared Tasks, create Worktrees, send teammate messages, or spawn more "
            "agents. Return JSON only with keys verdict, summary, changed_files, "
            "tests, remaining_risks. verdict must be complete or blocked."
        ),
        tool_names=("glob", "read_file", "write_file", "edit_file", "bash"),
        max_tool_rounds=6,
        max_read_paths=20,
        max_tool_calls=40,
        max_response_tokens=8000,
        read_only=False,
    ),
    "explore": AgentProfile(
        name="explore",
        description=(
            "Read-only temporary explorer that maps contracts, files, symbols, "
            "and producer-to-consumer relationships before implementation."
        ),
        instructions=(
            "Stay read-only. The Harness supplies a repository manifest; do not "
            "rediscover languages or file layout. In the single tool round, read "
            "task-relevant guidance/README plus the smallest relevant source set. "
            "Do not scan every source or test file merely because the lead asks "
            "for a complete map; use at most eight high-value paths. "
            "Trace real execution paths and "
            "derived values from producer through consumer. Distinguish verified "
            "facts from assumptions. Return JSON only with keys verdict, summary, "
            "requirements, code_map, risks, files_checked. verdict must be "
            "complete or blocked. Do not propose broad rewrites and do not edit."
        ),
        tool_names=("read_file",),
        max_tool_rounds=1,
        max_read_paths=8,
        max_tool_calls=10,
        max_response_tokens=4000,
    ),
    "plan": AgentProfile(
        name="plan",
        description=(
            "Read-only temporary planning agent that turns repository evidence "
            "into an ordered implementation and verification plan."
        ),
        instructions=(
            "Stay read-only. Inspect the relevant contract and the smallest source "
            "set needed to produce an actionable plan. Identify dependencies, "
            "ordering constraints, risks, and verification steps. Do not edit, "
            "create shared Tasks, or spawn agents. Return JSON only with keys "
            "verdict, summary, plan, risks, files_checked. verdict must be complete "
            "or blocked."
        ),
        tool_names=("glob", "read_file"),
        max_tool_rounds=3,
        max_read_paths=12,
        max_tool_calls=18,
        max_response_tokens=5000,
    ),
    "review": AgentProfile(
        name="review",
        description=(
            "Independent read-only temporary reviewer for final changes and "
            "uncovered contract requirements."
        ),
        instructions=(
            "Review independently from the implementing agent. Re-read the task "
            "and relevant contract even if the lead prompt names only its expected "
            "fixes; the root task is authoritative and the lead may not narrow it. "
            "Inspect the complete changed-file set plus only direct "
            "dependencies needed to verify them, and look for behavior regressions, "
            "missing fields, failure branches, atomicity, idempotency, state "
            "transitions, and API compatibility when relevant. Audit every README "
            "contract section represented in the lead checklist, not only failing "
            "public tests or changed files. For requirements naming every field or "
            "state component, enumerate the expected set and compare it with the "
            "producer implementation. Treat a required field omitted from a "
            "fingerprint, serialization, digest, receipt, or snapshot as a finding. "
            "Do not report a finding whose own evidence says the code is correct, "
            "acceptable, or already handles the case. Report at most five "
            "actionable findings; each finding has severity, requirement, file, "
            "symbol, and concise evidence. For every lead acceptance item that is "
            "fully supported by the inspected evidence, return its exact ID in "
            "verified_acceptance_ids; omit any ID affected by a finding or missing "
            "evidence. Return a compact JSON object only with keys verdict, summary, "
            "findings, files_checked, missing_evidence, "
            "verified_acceptance_ids. "
            "verdict must be pass, gaps, or blocked. A pass requires concrete code "
            "evidence and an empty findings list, not only public test success. Do "
            "not narrate chain-of-thought, use Markdown, or edit files."
        ),
        tool_names=("read_file",),
        max_tool_rounds=2,
        max_read_paths=16,
        max_tool_calls=20,
        max_response_tokens=8000,
    ),
}


_ROLE_ALIASES = {
    "general": "general-purpose",
    "general_purpose": "general-purpose",
    "explorer": "explore",
    "reviewer": "review",
}


def normalize_agent_role(role: str) -> str:
    normalized = str(role or "").strip().lower()
    return _ROLE_ALIASES.get(normalized, normalized)


def get_agent_profile(role: str) -> AgentProfile | None:
    return AGENT_PROFILES.get(normalize_agent_role(role))


def agent_profile_catalog() -> str:
    return "\n".join(
        f"- {profile.name}: {profile.description}"
        for profile in AGENT_PROFILES.values()
    )


def _has_intent_marker(text: str, marker: str) -> bool:
    if marker.isascii():
        return bool(re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(marker)}(?![A-Za-z0-9_])",
            text,
        ))
    return marker in text


def classify_delegation_intent(text: str) -> dict:
    """Classify a temporary subagent request without creating team state."""
    normalized = " ".join(str(text or "").lower().split())
    mutation_markers = (
        "implement", "modify", "edit", "write", "create", "add", "remove",
        "rename", "refactor", "repair", "fix", "patch", "update",
        "实现", "修改", "编辑", "编写", "创建", "新增", "删除", "重构", "修复",
    )
    review_markers = (
        "review", "audit", "final review", "review the changes", "find bugs",
        "regression risk", "correctness review", "security review",
        "审查", "审计", "复核", "检查改动", "检查最终", "回归风险", "找漏洞",
    )
    plan_markers = (
        "plan", "design", "approach", "implementation steps", "migration plan",
        "architecture plan",
    )
    exploration_markers = (
        "read", "inspect", "analyze", "investigate", "locate", "find",
        "trace", "map", "understand", "explain", "search", "identify",
        "阅读", "分析", "定位", "查找", "调查", "追踪", "梳理", "映射", "理解",
    )

    mutation_hits = [
        marker for marker in mutation_markers
        if _has_intent_marker(normalized, marker)
    ]
    review_hits = [
        marker for marker in review_markers
        if _has_intent_marker(normalized, marker)
    ]
    plan_hits = [
        marker for marker in plan_markers
        if _has_intent_marker(normalized, marker)
    ]
    exploration_hits = [
        marker for marker in exploration_markers
        if _has_intent_marker(normalized, marker)
    ]
    # A request to inspect and change code belongs to the bounded general-purpose
    # subagent. Review and plan stay read-only and take precedence over generic
    # exploration because they have stricter output contracts.
    if mutation_hits:
        role, hits = "general-purpose", mutation_hits
    elif review_hits:
        role, hits = "review", review_hits
    elif plan_hits:
        role, hits = "plan", plan_hits
    elif exploration_hits:
        role, hits = "explore", exploration_hits
    else:
        role, hits = "general-purpose", []
    return {
        "role": role,
        "reason": f"{role}_intent" if hits else "no_specialized_intent",
        "matched_markers": hits[:6],
    }


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
        f"complex ({reasons}). A temporary Explore or Plan subagent may be used "
        "when its bounded fresh context will replace broad Lead work; reuse its "
        "evidence instead of repeating the same reads. Temporary subagents do not "
        "join the shared Task pool and do not own Worktrees. For genuinely "
        "independent parallel work, the Lead may create shared Tasks and spawn "
        "persistent teammates. Every teammate keeps the common Task and mailbox "
        "protocol, may claim further unblocked Tasks, and remains alive until the "
        "Lead requests shutdown. After the final code change, the harness may run "
        "one temporary Review subagent automatically. Do not duplicate that "
        "review. The Lead owns decomposition, teammate lifecycle, integration, "
        "tests, and the final answer."
        "</multiagent_policy>"
    )
