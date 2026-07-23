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
    uses_worktree: bool = False


AGENT_PROFILES = {
    "general": AgentProfile(
        name="general",
        description=(
            "Small read-only helper for one focused question that does not need "
            "repository-wide exploration, implementation, or final review."
        ),
        instructions=(
            "Answer one bounded delegated question. Stay read-only and inspect "
            "only the few files needed for the answer; do not scan the repository "
            "or behave like an implementation worker. Return JSON only with keys "
            "verdict, summary, evidence, files_checked, remaining_questions. "
            "verdict must be complete or blocked."
        ),
        tool_names=("glob", "read_file"),
        max_tool_rounds=2,
        max_read_paths=8,
        max_tool_calls=10,
        max_response_tokens=4000,
    ),
    "explorer": AgentProfile(
        name="explorer",
        description=(
            "Read-only repository explorer that maps contracts, files, symbols, "
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
        max_tool_calls=8,
        max_response_tokens=4000,
    ),
    "reviewer": AgentProfile(
        name="reviewer",
        description=(
            "Independent read-only correctness reviewer for final changes and "
            "uncovered contract requirements."
        ),
        instructions=(
            "Review independently from the implementing agent. Re-read the task "
            "and relevant contract even if the lead prompt names only its expected "
            "fixes; the root task is authoritative and the lead may not narrow it. "
            "Inspect the complete changed-file set plus only direct "
            "dependencies needed to verify them, and look for behavior regressions, "
            "missing fields, failure branches, atomicity, idempotency, state "
            "transitions, and API compatibility when relevant. Report at most five "
            "actionable findings; each finding has severity, requirement, file, "
            "symbol, and concise evidence. Return a compact JSON object only with "
            "keys verdict, summary, findings, files_checked, missing_evidence. "
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
        max_tool_rounds=6,
        max_read_paths=20,
        max_tool_calls=48,
        max_response_tokens=8000,
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


def _has_intent_marker(text: str, marker: str) -> bool:
    if marker.isascii():
        return bool(re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(marker)}(?![A-Za-z0-9_])",
            text,
        ))
    return marker in text


def classify_delegation_intent(text: str) -> dict:
    """Route a legacy/general delegation by requested work, not case identity."""
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
    exploration_hits = [
        marker for marker in exploration_markers
        if _has_intent_marker(normalized, marker)
    ]
    # A request to both inspect and change code is implementation work. The
    # worker may inspect inside its worktree before editing. Pure review takes
    # precedence over exploration because it has stricter evidence semantics.
    if mutation_hits:
        role, hits = "worker", mutation_hits
    elif review_hits:
        role, hits = "reviewer", review_hits
    elif exploration_hits:
        role, hits = "explorer", exploration_hits
    else:
        role, hits = "general", []
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
        f"complex ({reasons}). Consider one early Explorer delegation only when "
        "an independent repository map is likely to replace broad Lead reads. "
        "Give it a focused question and reuse its verified paths and evidence; "
        "do not duplicate the same exploration in both contexts. Explorer is "
        "advisory and non-gating. Delegate a Worker only for one "
        "bounded implementation slice. delegate_agent(worker) owns Task and "
        "Worktree creation; do not call create_task/create_worktree first. Worker "
        "changes reach the main workspace only through integrate_worktree. After "
        "the final code change, the harness may run one pre-final reviewer "
        "automatically. Do not duplicate that review. Reviewer findings inform "
        "the lead but do not own final authority. "
        "The lead owns decomposition, integration, tests, and the final answer."
        "</multiagent_policy>"
    )
