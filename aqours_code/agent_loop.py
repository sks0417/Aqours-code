from __future__ import annotations

from .runtime_state import *

from pathlib import Path as _Path
import shutil as _shutil
import os as _os
import time as _time
from .command_executor import CaseTimeoutError as _CaseTimeoutError
from .agent_profiles import (
    assess_task_complexity,
    complex_delegation_briefing,
    normalize_agent_role,
)
from .model_budget import (
    can_spend_optional_calls,
    finalization_reserve_active,
)
from .runtime import AgentRuntime
from .model_api import (
    assistant_message_from_response,
    effective_escalated_max_tokens,
)

# ── Agent Loop ──

agent_lock = threading.Lock()
_MUTATING_FILE_TOOLS = {"write_file", "edit_file"}
_MAIN_MUTATION_TOOLS = _MUTATING_FILE_TOOLS | {"integrate_worktree"}


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_result":
                return ""
        elif getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "\n".join(parts)


def _latest_user_instruction(messages: list) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _message_text(message.get("content", ""))
        if text:
            return text
    return ""


def _runtime_todos(runtime: AgentRuntime | None = None) -> list[dict]:
    return runtime.state.todos if runtime is not None else CURRENT_TODOS


def _incomplete_todos(
    runtime: AgentRuntime | None = None,
) -> list[dict]:
    return [
        todo for todo in _runtime_todos(runtime)
        if todo.get("status") != "completed"
    ]


def _todo_completion_message(items: list[dict]) -> str:
    lines = "\n".join(
        f"- [{item.get('id', 'todo')} "
        f"{item.get('status', 'pending')}] {item.get('content', '')}"
        for item in items[:8]
    )
    return (
        "<todo_completion_reminder>You still have the following incomplete "
        "Todo items:\n"
        f"{lines}\n"
        "Complete them with the normal tools, or leave them pending and report "
        "the task as incomplete if they cannot be resolved."
        "</todo_completion_reminder>"
    )


def _append_todo_warning(content: list, items: list[dict]):
    lines = "\n".join(
        f"- [{item.get('id', 'todo')}] {item.get('content', '')}"
        for item in items[:8])
    content.append({
        "type": "text",
        "text": (
            "\n\n[Todo checklist incomplete]\n"
            "The following planned work remains incomplete:\n"
            f"{lines}"
        ),
    })


def _tool_json(output) -> dict:
    if isinstance(output, dict):
        return output
    try:
        value = json.loads(str(output))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _observed_lead_tests(messages: list, limit: int = 5) -> list[dict]:
    """Extract commands/results only, never Lead prose or reasoning."""
    pending: dict[str, str] = {}
    observed = []
    test_markers = (
        "pytest", "unittest", "python -m test", "python3 -m test", "tox",
        "nox", "npm test", "npm run test", "pnpm test", "yarn test",
        "cargo test", "go test", "dotnet test", "mvn test", "gradle test",
        "./gradlew test",
    )
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if message.get("role") == "assistant":
            for block in content:
                kind = (block.get("type") if isinstance(block, dict)
                        else getattr(block, "type", ""))
                name = (block.get("name") if isinstance(block, dict)
                        else getattr(block, "name", ""))
                if kind != "tool_use" or name != "bash":
                    continue
                data = (block.get("input", {}) if isinstance(block, dict)
                        else getattr(block, "input", {}) or {})
                command = str(data.get("command", "")).strip()
                normalized_command = " ".join(command.lower().split())
                if not (any(
                    marker in normalized_command
                    for marker in test_markers
                ) or ("assert " in normalized_command
                      and "python" in normalized_command)):
                    continue
                block_id = (block.get("id", "") if isinstance(block, dict)
                            else getattr(block, "id", ""))
                pending[str(block_id)] = command[:500]
        elif message.get("role") == "user":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                command = pending.pop(str(block.get("tool_use_id", "")), "")
                if not command:
                    continue
                output = " ".join(str(block.get("content", "")).split())
                failed = output.lower().startswith((
                    "error:", "permission denied", "tool not run:",
                )) or bool(re.search(r"\b(?:failed|failure|error)s?\b", output.lower()))
                observed.append({
                    "command": command,
                    "result": ("fail: " if failed else "pass: ") + output[:160],
                })
                if len(observed) >= limit:
                    return observed
    return observed


def _verification_feedback(outcome: dict) -> str:
    report = outcome.get("report", {})
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if outcome.get("status") == "findings":
        return (
            "Independent verification produced candidate findings.\n\n"
            "A candidate may be a real defect or a false positive; model-written "
            "evidence is not proof. Check each finding against the original task "
            "and README and reproduce it first. Fix real defects. A false "
            "positive does not require a code change, but it must be disproved "
            "with a new targeted test. Before finishing, run the complete public "
            "test suite. Confirmed findings additionally require their captured "
            "replayable failing commands to pass after a workspace change.\n\n"
            f"{rendered}"
        )
    reason = str(outcome.get("failure_reason") or "verifier_inconclusive")
    return (
        "Independent verification was inconclusive "
        f"({reason}). Perform focused self-verification against the original "
        "task and README, run the public test suite, and address any concrete "
        "issues before finishing. The independent verifier will not run again.\n\n"
        f"{rendered}"
    )


def _finding_state_counts(findings: list[dict]) -> dict[str, int]:
    counts = {
        "candidate": 0,
        "confirmed": 0,
        "dismissed": 0,
        "resolved": 0,
        "unresolved": 0,
    }
    for finding in findings:
        state = str(finding.get("state", "candidate"))
        if state in counts:
            counts[state] += 1
    return counts


def _test_command_signature(command: object) -> str:
    return " ".join(str(command or "").strip().split()).lower()


def _evaluate_verification_resolution(
    resolution: dict,
    current_snapshot: dict[str, str],
) -> dict:
    """Evaluate candidate findings from observable post-feedback facts."""
    findings = resolution.get("findings", [])
    verifier_tests = {
        str(test.get("id")): test
        for test in resolution.get("verifier_tests", [])
        if isinstance(test, dict) and test.get("id")
    }
    leader_tests = [
        test for test in resolution.get("leader_tests", [])
        if isinstance(test, dict)
    ]
    public_passed = any(
        test.get("scope") == "public_suite" and test.get("exit_code") == 0
        for test in leader_tests
    )
    targeted_passes = [
        test for test in leader_tests
        if test.get("scope") == "targeted" and test.get("exit_code") == 0
    ]
    feedback_snapshot = resolution.get("feedback_snapshot", {})
    code_changed = any(
        feedback_snapshot.get(path) != current_snapshot.get(path)
        for path in set(feedback_snapshot) | set(current_snapshot)
    )
    candidate_total = sum(
        finding.get("state") == "candidate" for finding in findings
    )
    targets = []
    for finding in findings:
        origin = str(finding.get("state", "candidate"))
        if origin == "confirmed":
            required = [
                verifier_tests[test_id]
                for test_id in finding.get("evidence_test_ids", [])
                if test_id in verifier_tests
                and verifier_tests[test_id].get("replayable") is True
                and verifier_tests[test_id].get("result") == "fail"
            ]
            replay_passed = bool(required) and all(
                any(
                    _test_command_signature(test.get("command", ""))
                    == _test_command_signature(required_test.get("command", ""))
                    and test.get("exit_code") == 0
                    for test in leader_tests
                )
                for required_test in required
            )
            state = (
                "resolved"
                if code_changed and public_passed and replay_passed
                else "unresolved"
            )
            reason = (
                "replay_passed_after_workspace_change"
                if state == "resolved"
                else "confirmed_failure_not_resolved"
            )
        elif code_changed:
            enough_targeted = len(targeted_passes) >= max(1, candidate_total)
            state = (
                "resolved" if public_passed and enough_targeted
                else "unresolved"
            )
            reason = (
                "targeted_and_public_passed_after_workspace_change"
                if state == "resolved"
                else "candidate_resolution_evidence_incomplete"
            )
        else:
            enough_targeted = len(targeted_passes) >= max(1, candidate_total)
            state = (
                "dismissed" if public_passed and enough_targeted
                else "candidate"
            )
            reason = (
                "targeted_counterevidence_and_public_suite_passed"
                if state == "dismissed"
                else "candidate_not_addressed"
            )
        targets.append({
            "finding": finding,
            "to": state,
            "reason": reason,
        })
    success = bool(targets) and all(
        item["to"] in {"resolved", "dismissed"} for item in targets
    )
    return {
        "success": success,
        "targets": targets,
        "code_changed": code_changed,
        "public_suite_run": any(
            test.get("scope") == "public_suite" for test in leader_tests
        ),
        "public_suite_passed": public_passed,
        "targeted_tests_passed": len(targeted_passes),
    }


def _apply_resolution_transitions(
    resolution: dict,
    evaluation: dict,
    *,
    incomplete: bool = False,
) -> str:
    findings = resolution.get("findings", [])
    for item in evaluation.get("targets", []):
        finding = item["finding"]
        old_state = str(finding.get("state", "candidate"))
        new_state = str(item["to"])
        if incomplete and new_state == "candidate":
            new_state = "candidate"
        if old_state == new_state:
            continue
        finding["state"] = new_state
        record_event(
            "verification_finding_transition",
            finding_id=finding.get("id", ""),
            **{"from": old_state, "to": new_state},
            reason=item.get("reason", ""),
        )
    counts = _finding_state_counts(findings)
    if incomplete:
        resolution_status = (
            "unresolved" if counts["unresolved"] else "incomplete"
        )
    elif counts["resolved"]:
        resolution_status = "resolved"
    else:
        resolution_status = "dismissed"
    record_event(
        "verification_resolution",
        resolution_status=resolution_status,
        candidate_findings=counts["candidate"],
        confirmed_findings=counts["confirmed"],
        dismissed_findings=counts["dismissed"],
        resolved_findings=counts["resolved"],
        unresolved_findings=counts["unresolved"],
        public_suite_run=bool(evaluation.get("public_suite_run")),
        public_suite_passed=bool(evaluation.get("public_suite_passed")),
        workspace_modified_after_findings=bool(evaluation.get("code_changed")),
    )
    return resolution_status


def _resolution_reminder(evaluation: dict) -> str:
    return (
        "<verification_resolution_reminder>Independent verification findings "
        "remain unresolved. A normal success final cannot be accepted yet. "
        "For a real defect, update the workspace, rerun every captured "
        "confirmed/replayable failure, and run the complete public suite. For "
        "a false-positive candidate, run new targeted counterevidence and the "
        "complete public suite; no code change is required. "
        f"Observed after feedback: workspace_changed={bool(evaluation.get('code_changed'))}, "
        f"targeted_passes={int(evaluation.get('targeted_tests_passed', 0))}, "
        f"public_suite_passed={bool(evaluation.get('public_suite_passed'))}."
        "</verification_resolution_reminder>"
    )


_REVIEW_FINDING_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "both", "by", "does",
    "for", "from", "in", "is", "it", "must", "not", "of", "on", "or",
    "that", "the", "this", "to", "when", "with",
}
_REVIEW_FINDING_ROOT_WORDS = {
    "allocation", "atomic", "balance", "batch", "checkpoint", "currency",
    "digest", "duplicate", "event", "exactly", "fingerprint", "idempotency",
    "ingestion", "partition", "receipt", "recovery", "replay", "restore",
    "rollback", "sequence", "serialization", "snapshot", "state",
    "transaction", "validation",
}
_REVIEW_SEVERITY_RANK = {
    "warning": 0,
    "minor": 1,
    "major": 2,
    "critical": 3,
}


def _reviewer_path_key(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    path = re.sub(r":\d+(?:-\d+)?$", "", path)
    while path.startswith("./"):
        path = path[2:]
    if path.lower().startswith("/workspace/"):
        path = path[len("/workspace/"):]
    return path.strip("/").lower()


def _reviewer_text_tokens(value: object) -> set[str]:
    return {
        token for token in re.findall(
            r"[a-z0-9_]+", str(value or "").lower()
        )
        if len(token) >= 3 and token not in _REVIEW_FINDING_STOP_WORDS
    }


def _reviewer_token_overlap(left: object, right: object) -> tuple[int, float]:
    left_tokens = _reviewer_text_tokens(left)
    right_tokens = _reviewer_text_tokens(right)
    if not left_tokens or not right_tokens:
        return 0, 0.0
    shared = len(left_tokens & right_tokens)
    return shared, shared / min(len(left_tokens), len(right_tokens))


def _reviewer_checked_path(path: str, checked_paths: set[str]) -> bool:
    key = _reviewer_path_key(path)
    return bool(key and any(
        key == checked
        or key.endswith(f"/{checked}")
        or checked.endswith(f"/{key}")
        for checked in checked_paths if checked
    ))


def _reviewer_finding_file(
    path: str,
    runtime: AgentRuntime | None,
) -> _Path | None:
    if runtime is None:
        return None
    cleaned = re.sub(
        r":\d+(?:-\d+)?$", "", str(path or "").strip().replace("\\", "/")
    )
    if cleaned.lower().startswith("/workspace/"):
        cleaned = cleaned[len("/workspace/"):]
    candidate = _Path(cleaned)
    if not candidate.is_absolute():
        candidate = runtime.paths.workdir / cleaned
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if (
        not resolved.is_relative_to(runtime.paths.workdir)
        or not resolved.is_file()
    ):
        return None
    return resolved


def _reviewer_self_negating_evidence(evidence: object) -> bool:
    text = " ".join(str(evidence or "").lower().split())
    patterns = (
        r"\b(?:this|that|the)\s+(?:code|implementation|behavior|logic)\s+"
        r"(?:is|appears)\s+(?:already\s+)?(?:correct|acceptable|handled)\b",
        r"\b(?:is|was)\s+already\s+(?:handled|implemented|validated|verified)\b",
        r"\bverified\s+(?:as\s+)?(?:correct|acceptable)\b",
        r"\bno\s+(?:hidden\s+)?defect\s+(?:exists|was found|is present)\b",
        r"\bno\s+(?:actual\s+)?defect\b",
        r"\bfinding\s+(?:is\s+)?withdrawn\b",
        r"\bwithdraw(?:n)?\b",
        r"\bno\s+finding\b",
        r"\b(?:contract|requirement)\s+(?:is|appears\s+)?satisfied\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _reviewer_unsupported_absence_evidence(evidence: object) -> bool:
    """Reject repository-wide absence claims that provide no inspected proof."""
    text = " ".join(str(evidence or "").lower().split())
    return bool(
        re.search(r"\b(?:there\s+is\s+)?no\s+evidence\b", text)
        or re.search(r"\bnot\s+implemented\s+anywhere\b", text)
    )


def _reviewer_findings_duplicate(
    left: dict,
    right: dict,
) -> bool:
    if _reviewer_path_key(left.get("file")) != _reviewer_path_key(
        right.get("file")
    ):
        return False
    left_symbol = str(left.get("symbol", "")).strip().lower()
    right_symbol = str(right.get("symbol", "")).strip().lower()
    if left_symbol != right_symbol:
        return False
    left_requirement = " ".join(
        str(left.get("requirement", "")).lower().split()
    )
    right_requirement = " ".join(
        str(right.get("requirement", "")).lower().split()
    )
    if left_requirement and (
        left_requirement == right_requirement
        or (
            min(len(left_requirement), len(right_requirement)) >= 30
            and (
                left_requirement in right_requirement
                or right_requirement in left_requirement
            )
        )
    ):
        return True
    combined_left = (
        f"{left.get('requirement', '')} {left.get('evidence', '')}"
    )
    combined_right = (
        f"{right.get('requirement', '')} {right.get('evidence', '')}"
    )
    shared, overlap = _reviewer_token_overlap(combined_left, combined_right)
    root_overlap = (
        _reviewer_text_tokens(combined_left)
        & _reviewer_text_tokens(combined_right)
        & _REVIEW_FINDING_ROOT_WORDS
    )
    return bool(
        shared >= 4
        and (overlap >= 0.55 or (len(root_overlap) >= 3 and overlap >= 0.35))
    )


def _reviewer_trace_finding(finding: dict) -> dict:
    return {
        "severity": str(finding.get("severity", "warning"))[:30],
        "requirement": str(finding.get("requirement", ""))[:500],
        "file": str(finding.get("file", ""))[:260],
        "symbol": str(finding.get("symbol", ""))[:160],
        "evidence": str(finding.get("evidence", ""))[:500],
    }


def _screen_reviewer_findings(
    delegation: dict,
    runtime: AgentRuntime | None = None,
) -> dict:
    """Validate and deduplicate Reviewer findings before returning them to Lead."""
    result = delegation.get("result", {})
    if not isinstance(result, dict):
        result = {}
    raw_findings = result.get("findings", [])
    if not isinstance(raw_findings, list):
        raw_findings = [raw_findings] if raw_findings else []
    checked = result.get("files_checked", [])
    if not isinstance(checked, list):
        checked = [checked] if checked else []
    checked_paths = {
        _reviewer_path_key(path) for path in checked[:20]
        if _reviewer_path_key(path)
    }

    accepted: list[dict] = []
    suppressed: list[dict] = []
    for raw_index, raw_finding in enumerate(raw_findings[:5], 1):
        if not isinstance(raw_finding, dict):
            continue
        finding = dict(raw_finding)
        evidence = str(finding.get("evidence", "")).strip()
        path = str(finding.get("file", "")).strip()
        reason = ""
        if not evidence:
            reason = "missing_evidence"
        elif not path:
            reason = "missing_file"
        elif not _reviewer_checked_path(path, checked_paths):
            reason = "file_not_checked"
        elif _reviewer_self_negating_evidence(evidence):
            reason = "self_negating_evidence"
        elif _reviewer_unsupported_absence_evidence(evidence):
            reason = "unsupported_absence_claim"

        candidate = None
        if not reason and runtime is not None:
            candidate = _reviewer_finding_file(path, runtime)
            if candidate is None:
                reason = "file_not_found"
        symbol = str(finding.get("symbol", "")).strip()
        if not reason and candidate is not None and symbol:
            symbol_parts = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", symbol)
            symbol_name = symbol_parts[-1] if symbol_parts else ""
            if symbol_name and symbol_name.lower() not in {"module", "file"}:
                try:
                    source = candidate.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    reason = "file_not_readable"
                else:
                    if re.search(
                        rf"\b{re.escape(symbol_name)}\b", source
                    ) is None:
                        reason = "symbol_not_found"

        duplicate_of = 0
        if not reason:
            for accepted_index, existing in enumerate(accepted, 1):
                if _reviewer_findings_duplicate(existing, finding):
                    duplicate_of = accepted_index
                    reason = "semantic_duplicate"
                    current_severity = str(
                        existing.get("severity", "warning")
                    ).lower()
                    incoming_severity = str(
                        finding.get("severity", "warning")
                    ).lower()
                    if _REVIEW_SEVERITY_RANK.get(
                        incoming_severity, 0
                    ) > _REVIEW_SEVERITY_RANK.get(current_severity, 0):
                        existing["severity"] = incoming_severity
                    incoming_evidence = str(
                        finding.get("evidence", "")
                    ).strip()
                    existing_evidence = str(
                        existing.get("evidence", "")
                    ).strip()
                    if (
                        incoming_evidence
                        and incoming_evidence not in existing_evidence
                    ):
                        existing["evidence"] = (
                            f"{existing_evidence}; {incoming_evidence}"
                        ).strip("; ")[:500]
                    break

        if reason:
            trace_item = {
                "raw_index": raw_index,
                "reason": reason,
                "finding": _reviewer_trace_finding(finding),
            }
            if duplicate_of:
                trace_item["duplicate_of_accepted_index"] = duplicate_of
            suppressed.append(trace_item)
            continue

        accepted.append(finding)

    record_event(
        "reviewer_screening",
        decision="reviewer_findings_screened",
        raw_count=len(raw_findings[:5]),
        accepted_count=len(accepted),
        suppressed_count=len(suppressed),
        suppressed_findings=suppressed,
    )
    return {
        "findings": accepted,
        "suppressed": suppressed,
        "raw_count": len(raw_findings[:5]),
    }


def _screened_reviewer_output(
    delegation: dict,
    screening: dict,
) -> str:
    """Give the Lead only actionable findings while preserving raw output in trace."""
    payload = dict(delegation)
    raw_result = delegation.get("result", {})
    result = dict(raw_result) if isinstance(raw_result, dict) else {}
    actionable = []
    for finding in screening.get("findings", [])[:5]:
        if not isinstance(finding, dict):
            continue
        actionable.append({
            key: value for key, value in finding.items()
            if not str(key).startswith("_")
        })
    result["findings"] = actionable
    payload["result"] = result
    payload["screening"] = {
        "raw_finding_count": int(screening.get("raw_count", 0)),
        "actionable_finding_count": len(actionable),
        "suppressed_finding_count": len(screening.get("suppressed", [])),
        "note": (
            "Only actionable findings are included in result.findings. "
            "Suppressed findings are retained in the execution trace and must "
            "not be re-audited by the Lead."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def _finalization_budget_message(snapshot: dict) -> str:
    return (
        "<finalization_budget>This task has entered its reserved finalization "
        f"budget ({snapshot.get('remaining_calls')} model calls remain; "
        f"{snapshot.get('reserve_calls')} are reserved). Do not start a new "
        "Explore, Plan, Review, persistent Teammate, broad repository scan, or "
        "model-generated "
        "compact summary. Continue directly from retained evidence. Use the "
        "remaining calls only for unresolved fixes, targeted verification, and "
        "one final answer.</finalization_budget>"
    )


def _context_stats(
    messages: list,
    *,
    system: str = "",
    tools: list | None = None,
    dynamic: dict | None = None,
) -> dict:
    estimated_size = estimate_context_size(
        messages,
        system=system,
        tools=tools,
        dynamic=dynamic,
    )
    return {
        "message_count": len(messages),
        "estimated_size": estimated_size,
        "estimated_tokens": estimate_context_tokens(estimated_size),
    }


def _compact_target_chars() -> int:
    return COMPACT_TRIGGER_TOKENS * CONTEXT_CHARS_PER_TOKEN


def _latest_genuine_user_signature(messages: list) -> str:
    for message in reversed(messages):
        if message.get("role") != "user" or is_tool_result_message(message):
            continue
        text = _message_text(message.get("content", "")).lstrip()
        if text.startswith(CONTEXT_CHECKPOINT_MARKER):
            continue
        return json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
    return ""


def _record_context_integrity(messages: list, latest_user_before: str) -> None:
    tool_use_ids = set()
    tool_result_ids = set()
    checkpoint_indexes = []
    for index, message in enumerate(messages):
        if _message_text(message.get("content", "")).lstrip().startswith(
                CONTEXT_CHECKPOINT_MARKER):
            checkpoint_indexes.append(index)
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block_type(block) == "tool_use":
                value = (block.get("id") if isinstance(block, dict)
                         else getattr(block, "id", ""))
                if value:
                    tool_use_ids.add(str(value))
            elif block_type(block) == "tool_result":
                value = (block.get("tool_use_id") if isinstance(block, dict)
                         else getattr(block, "tool_use_id", ""))
                if value:
                    tool_result_ids.add(str(value))
    latest_after = _latest_genuine_user_signature(messages)
    record_event(
        "context_integrity",
        message_count=len(messages),
        checkpoint_present=bool(checkpoint_indexes),
        checkpoint_indexes=checkpoint_indexes,
        latest_user_preserved=bool(
            latest_user_before and latest_after == latest_user_before),
        orphan_tool_result_ids=sorted(tool_result_ids - tool_use_ids),
    )


def _run_context_stage(stage: str, messages: list, func) -> list:
    before = _context_stats(messages)
    next_messages = func(messages)
    after = _context_stats(next_messages)
    changed = (before != after)
    if changed:
        record_event(
            "context_compact",
            stage=stage,
            changed=True,
            before_messages=before["message_count"],
            after_messages=after["message_count"],
            before_size=before["estimated_size"],
            after_size=after["estimated_size"],
        )
    return next_messages


def _request_sizer(
    base_context: dict,
    tools: list,
    runtime: AgentRuntime | None,
):
    def size(candidate: list) -> int:
        refreshed = (
            update_context(base_context, candidate, runtime)
            if runtime is not None else update_context(base_context, candidate)
        )
        assembled = (
            assemble_system_prompt(refreshed, runtime)
            if runtime is not None else assemble_system_prompt(refreshed)
        )
        return estimate_context_size(
            candidate,
            system=assembled,
            tools=tools,
        )
    return size


def prepare_context(
    messages: list,
    runtime: AgentRuntime | None = None,
    context: dict | None = None,
    tools: list | None = None,
) -> list:
    # A per-result hard limit applies before both complete-request sizing and
    # the next provider call. This deterministic replacement is independent of
    # whether semantic compaction reaches its trigger.
    raw_messages = list(messages)
    sanitized_messages, sanitized_count = sanitize_context_tool_results(
        messages
    )
    if sanitized_count:
        messages[:] = sanitized_messages

    budget_context = (
        context
        if context is not None
        else (
            update_context({}, messages, runtime)
            if runtime is not None else update_context({}, messages)
        )
    )
    budget_tools = tools
    if budget_tools is None:
        budget_tools, _ = (
            assemble_tool_pool(runtime)
            if runtime is not None else assemble_tool_pool()
        )
    system = (
        assemble_system_prompt(budget_context, runtime)
        if runtime is not None else assemble_system_prompt(budget_context)
    )
    if sanitized_count:
        raw_stats = _context_stats(
            raw_messages,
            system=system,
            tools=budget_tools,
        )
        sanitized_stats = _context_stats(
            messages,
            system=system,
            tools=budget_tools,
        )
        record_event(
            "context_compact",
            stage="tool_result_limit",
            changed=True,
            summary_attempted=False,
            omitted_tool_result_count=sanitized_count,
            before_messages=raw_stats["message_count"],
            after_messages=sanitized_stats["message_count"],
            before_size=raw_stats["estimated_size"],
            after_size=sanitized_stats["estimated_size"],
            before_tokens=raw_stats["estimated_tokens"],
            after_tokens=sanitized_stats["estimated_tokens"],
        )
    before = _context_stats(messages, system=system, tools=budget_tools)
    record_event(
        "context_budget",
        message_count=before["message_count"],
        estimated_size=before["estimated_size"],
        estimated_tokens=before["estimated_tokens"],
        system_size=len(system),
        tool_schema_size=estimate_size(budget_tools),
    )
    if before["estimated_tokens"] >= COMPACT_TRIGGER_TOKENS:
        latest_user_before = _latest_genuine_user_signature(messages)
        sizer = _request_sizer(budget_context, budget_tools, runtime)
        compacted = (
            compact_history(
                messages,
                runtime=runtime,
                reason="automatic",
                target_context_budget=_compact_target_chars(),
                request_size_fn=sizer,
            )
            if runtime is not None else compact_history(
                messages,
                reason="automatic",
                target_context_budget=_compact_target_chars(),
                request_size_fn=sizer,
            )
        )
        changed = compacted != messages
        messages[:] = compacted
        refreshed_context = (
            update_context(budget_context, messages, runtime)
            if runtime is not None else update_context(budget_context, messages)
        )
        refreshed_system = (
            assemble_system_prompt(refreshed_context, runtime)
            if runtime is not None else assemble_system_prompt(refreshed_context)
        )
        after = _context_stats(
            messages,
            system=refreshed_system,
            tools=budget_tools,
        )
        record_event(
            "context_compact",
            stage="compact_history",
            changed=changed,
            before_messages=before["message_count"],
            after_messages=after["message_count"],
            before_size=before["estimated_size"],
            after_size=after["estimated_size"],
            before_tokens=before["estimated_tokens"],
            after_tokens=after["estimated_tokens"],
        )
        if changed:
            _record_context_integrity(messages, latest_user_before)
    return messages


def build_user_content(results: list[dict]) -> list[dict]:
    # Tool results and completed background notifications are both returned to
    # the model as user-side content, matching the tool_result feedback loop.
    content = list(results)
    notes = collect_background_results()
    _record_background_notifications(notes, "tool_result_batch")
    for note in notes:
        content.append({"type": "text", "text": note})
    return content


def _record_background_notifications(notes: list, injection: str):
    for note in notes:
        record_event(
            "task_notification",
            injection=injection,
            task_id=getattr(note, "task_id", ""),
            status=getattr(note, "status", ""),
            command=getattr(note, "command", ""),
            summary=getattr(note, "summary", str(note)),
            original_size=getattr(note, "original_size", len(str(note))),
            truncated=bool(getattr(note, "truncated", False)),
        )


def inject_background_notifications(messages: list):
    notes = collect_background_results()
    if notes:
        _record_background_notifications(notes, "loop_start")
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})


def is_permission_denied_output(output) -> bool:
    return str(output).lower().startswith("permission denied")


def is_recoverable_tool_rejection(output) -> bool:
    return (isinstance(output, dict)
            and output.get("kind") == "tool_policy_rejection"
            and output.get("recoverable") is True)


def tool_rejection_text(output) -> str:
    if not (isinstance(output, dict)
            and output.get("kind") == "tool_policy_rejection"):
        return str(output)
    guidance = output.get("guidance", "")
    text = output.get("message", "Tool not run by policy.")
    if guidance:
        text += f"\nGuidance: {guidance}"
    return text


def _runtime_role_benefit(read_counts: dict[str, int], model_client) -> dict:
    """Describe a conservative, evidence-based opportunity for one Explorer."""
    unique_paths = len(read_counts)
    repeated_reads = sum(max(0, count - 1) for count in read_counts.values())
    scopes = set()
    for path in read_counts:
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        if parts and parts[0].lower() == "workspace":
            parts = parts[1:]
        scopes.add(parts[0] if len(parts) > 1 else "(root)")
    repeated_paths = sorted(
        path for path, count in read_counts.items() if count > 1)
    evidence_ready = (
        unique_paths >= 8 and repeated_reads >= 2 and len(scopes) >= 2)
    budget_allowed = False
    budget = {"available": False}
    if evidence_ready:
        # Two focused Explorer calls plus a three-call Reviewer allowance must
        # fit before the existing finalization reserve.
        budget_allowed, budget = can_spend_optional_calls(model_client, 5)
    return {
        "eligible": evidence_ready and budget_allowed,
        "evidence_ready": evidence_ready,
        "budget_allowed": budget_allowed,
        "unique_read_paths": unique_paths,
        "repeated_reads": repeated_reads,
        "scope_count": len(scopes),
        "repeated_paths": repeated_paths[:4],
        "budget": budget,
    }


def stop_after_permission_denied(messages: list, reason: str):
    if messages and messages[-1].get("role") == "assistant":
        messages[-1]["content"] = [{"type": "text", "text": reason}]
    else:
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": reason}
        ]})
    record_hook("Stop")
    trigger_hooks("Stop", messages)


def scheduled_prompt_text(job) -> str:
    label = "Scheduled Once" if getattr(job, "kind", "") == "once" else "Scheduled"
    return f"[{label}] {job.prompt}"


def call_llm(messages: list, context: dict, tools: list,
             state: RecoveryState, max_tokens: int,
             runtime: AgentRuntime | None = None):
    remaining = _remaining_case_time(runtime)
    if remaining is not None and remaining <= 0:
        _check_case_deadline(runtime)
    system = (
        assemble_system_prompt(context, runtime)
        if runtime is not None else assemble_system_prompt(context)
    )
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    record_llm_request(model=state.current_model, max_tokens=max_tokens,
                       message_count=len(messages), tool_count=len(tools))
    old_timeout = _os.environ.get("AQOURS_CODE_REQUEST_TIMEOUT")
    if remaining is not None:
        try:
            configured = float(old_timeout or "30")
        except (TypeError, ValueError):
            configured = 30.0
        _os.environ["AQOURS_CODE_REQUEST_TIMEOUT"] = str(max(0.1, min(configured, remaining)))
    try:
        return with_retry(
            lambda: model_client.messages.create(
                model=state.current_model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens),
            state)
    finally:
        if remaining is not None:
            if old_timeout is None:
                _os.environ.pop("AQOURS_CODE_REQUEST_TIMEOUT", None)
            else:
                _os.environ["AQOURS_CODE_REQUEST_TIMEOUT"] = old_timeout


def _remaining_case_time(
    runtime: AgentRuntime | None = None,
) -> float | None:
    deadline = runtime.state.deadline if runtime is not None else CASE_DEADLINE
    if deadline is None:
        return None
    return deadline - _time.monotonic()


def _check_case_deadline(runtime: AgentRuntime | None = None):
    remaining = _remaining_case_time(runtime)
    if remaining is not None and remaining <= 0:
        if background_workers_alive():
            raise _CaseTimeoutError(
                "eval case deadline exceeded while waiting for background tasks")
        raise _CaseTimeoutError("eval case deadline exceeded")


def agent_loop(
    messages: list,
    context: dict,
    runtime: AgentRuntime | None = None,
):
    from . import bootstrap
    bootstrap()
    tools, handlers = (
        assemble_tool_pool(runtime)
        if runtime is not None else assemble_tool_pool()
    )
    state = RecoveryState()
    if runtime is not None:
        state.current_model = runtime.config.primary_model
    max_tokens = DEFAULT_MAX_TOKENS
    # Todos are scoped to one user/cron turn. Incomplete items remain available
    # through every context compaction inside this loop.
    current_todos = _runtime_todos(runtime)
    current_todos.clear()
    if runtime is not None:
        runtime.state.metadata["compact_generation"] = 0
    todo_completion_followup_sent = False
    changed_file_paths = (
        runtime.state.changed_files if runtime is not None else set()
    )
    changed_file_paths.clear()
    lead_read_paths: set[str] = set()
    lead_read_counts = (
        runtime.state.lead_read_counts if runtime is not None else {}
    )
    lead_read_counts.clear()
    root_task = str(
        runtime.state.root_task if runtime is not None
        else CURRENT_ROOT_TASK or _latest_user_instruction(messages)
    )
    if not root_task:
        root_task = _latest_user_instruction(messages)
    complexity = assess_task_complexity(root_task)
    from .subagent import (
        _bash_exit_code,
        captured_test_fact,
        is_test_command,
        snapshot_workspace,
        workspace_changes,
    )
    verification_workdir = (
        runtime.paths.workdir if runtime is not None else _Path(WORKDIR)
    )
    task_start_snapshot = snapshot_workspace(verification_workdir)
    verifier_runs = 0
    verifier_run_limit = min(max(int(VERIFIER_MAX_RUNS_PER_TASK), 0), 1)
    verification_resolution = None
    multiagent_enabled = "delegate_agent" in handlers
    multiagent_required = (
        multiagent_enabled
        and complexity["level"] == "complex"
        and complexity.get("implementation_task", False)
    )
    explorer_attempted = False
    runtime_benefit_signal_sent = False
    explorer_cached_result = ""
    mutation_revision = 0
    reviewer_attempted_revision = -1
    reviewer_cached_result = ""
    finalization_budget_notice_sent = False
    budget_snapshot_observed = False
    if multiagent_required:
        messages.append({
            "role": "user", "content": complex_delegation_briefing(complexity),
        })
        record_event(
            "multiagent_policy", decision="advisory", **complexity)

    while True:
        _check_case_deadline(runtime)
        # One cycle: inject scheduled/background work, prepare context, call
        # the model, execute tool_use blocks, append tool_results, repeat.
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user",
                             "content": scheduled_prompt_text(job)})
            prefix = "once inject" if getattr(job, "kind", "") == "once" else "cron inject"
            print(f"  \033[35m[{prefix}] {job.prompt[:60]}\033[0m")

        inject_background_notifications(messages)

        model_client = (
            runtime.services.model_client if runtime is not None else client
        )
        reserve_active, budget_snapshot = finalization_reserve_active(
            model_client)
        if budget_snapshot.get("available") and not budget_snapshot_observed:
            budget_snapshot_observed = True
            record_event(
                "model_budget_guard", decision="budget_snapshot_available",
                **{key: value for key, value in budget_snapshot.items()
                   if key != "available"},
            )
        if (budget_snapshot.get("available")
                and budget_snapshot["remaining_calls"] <= 0):
            unresolved = _incomplete_todos(runtime)
            fallback = (
                "Harness stopped before issuing an over-budget model request. "
                "The implementation changes made so far remain in the workspace."
            )
            if unresolved:
                fallback += " Unresolved Todo work: " + "; ".join(
                    str(item.get("content", ""))[:180]
                    for item in unresolved[:5]
                )
            record_event(
                "model_budget_guard", decision="over_budget_request_prevented",
                unresolved_count=len(unresolved),
                **{key: value for key, value in budget_snapshot.items()
                   if key != "available"},
            )
            record_hook("Stop")
            trigger_hooks("Stop", messages)
            finish_run(fallback)
            return

        force_final_response = bool(
            budget_snapshot.get("available")
            and budget_snapshot["remaining_calls"] == 1
        )
        if reserve_active and not finalization_budget_notice_sent:
            messages.append({
                "role": "user",
                "content": _finalization_budget_message(budget_snapshot),
            })
            finalization_budget_notice_sent = True
            record_event(
                "model_budget_guard", decision="finalization_reserve_entered",
                **{key: value for key, value in budget_snapshot.items()
                   if key != "available"},
            )
        if force_final_response:
            unresolved = _incomplete_todos(runtime)
            verifier_deadline_note = ""
            if (
                complexity.get("implementation_task", False)
                and int(complexity.get("score", 0))
                >= VERIFIER_COMPLEXITY_THRESHOLD
                and verifier_run_limit > 0
                and verifier_runs < verifier_run_limit
                and workspace_changes(
                    task_start_snapshot,
                    snapshot_workspace(verification_workdir),
                )
            ):
                verifier_runs += 1
                verifier_deadline_note = (
                    " The independent verifier cannot run because the model-call "
                    "budget is exhausted. Self-check the implementation against "
                    "the original task and report any unverified risk honestly."
                )
                record_event(
                    "verification_skipped",
                    verification_skipped_reason="insufficient_model_budget",
                    complexity_score=int(complexity.get("score", 0)),
                    threshold=VERIFIER_COMPLEXITY_THRESHOLD,
                )
            messages.append({
                "role": "user",
                "content": (
                    "<finalization_deadline>Exactly one model call remains. "
                    "Tools are disabled for this call. Return the best accurate "
                    "final answer now from retained context; state any unfinished "
                    "Todo work honestly and do not request another action."
                    + (
                        " Unresolved Todo work: " + "; ".join(
                            str(item.get("content", ""))[:180]
                            for item in unresolved[:5]
                        )
                        if unresolved else ""
                    )
                    + verifier_deadline_note
                    + "</finalization_deadline>"
                ),
            })
            record_event(
                "model_budget_guard", decision="last_call_forced_final",
                unresolved_count=len(unresolved),
                **{key: value for key, value in budget_snapshot.items()
                   if key != "available"},
            )

        if runtime is not None:
            context = update_context(context, messages, runtime)
            tools, handlers = assemble_tool_pool(runtime)
            prepare_context(messages, runtime, context, tools)
            context = update_context(context, messages, runtime)
        else:
            context = update_context(context, messages)
            tools, handlers = assemble_tool_pool()
            # Keep the legacy entry point one-argument compatible for embedders
            # that replace prepare_context, while the function itself rebuilds
            # the same context/tool request components for budgeting.
            prepare_context(messages)
            context = update_context(context, messages)
        if force_final_response:
            tools = []

        try:
            response = (
                call_llm(messages, context, tools, state, max_tokens, runtime)
                if runtime is not None
                else call_llm(messages, context, tools, state, max_tokens)
            )
            record_llm_response(response)
        except Exception as e:
            if isinstance(e, _CaseTimeoutError):
                raise
            record_error(e)
            if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                reactive_sizer = _request_sizer(context, tools, runtime)
                messages[:] = (
                    reactive_compact(
                        messages,
                        runtime,
                        target_context_budget=_compact_target_chars(),
                        request_size_fn=reactive_sizer,
                    )
                    if runtime is not None else reactive_compact(
                        messages,
                        target_context_budget=_compact_target_chars(),
                        request_size_fn=reactive_sizer,
                    )
                )
                state.has_attempted_reactive_compact = True
                continue
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        if response.stop_reason == "max_tokens":
            replayable_response = bool(
                extract_text(response.content)
                or has_tool_use(response.content)
            )
            if force_final_response:
                if replayable_response:
                    messages.append(assistant_message_from_response(response))
                record_hook("Stop")
                trigger_hooks("Stop", messages)
                finish_run(extract_text(response.content))
                return
            if not state.has_escalated:
                provider_name = (
                    runtime.config.model_provider
                    if runtime is not None else MODEL_PROVIDER
                )
                max_tokens = effective_escalated_max_tokens(
                    provider_name,
                    state.current_model,
                    current_max_tokens=max_tokens,
                    configured_escalated_max_tokens=ESCALATED_MAX_TOKENS,
                )
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                continue
            # Some thinking models can spend the entire response budget on
            # hidden reasoning and return no text or tool call. Replaying that
            # as an empty assistant message is invalid for OpenAI-compatible
            # APIs and needlessly consumes the context budget.
            if replayable_response:
                messages.append(assistant_message_from_response(response))
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            return

        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append(assistant_message_from_response(response))
        if force_final_response:
            if verification_resolution is not None:
                evaluation = _evaluate_verification_resolution(
                    verification_resolution,
                    snapshot_workspace(verification_workdir),
                )
                if evaluation["success"]:
                    _apply_resolution_transitions(
                        verification_resolution, evaluation,
                    )
                else:
                    _apply_resolution_transitions(
                        verification_resolution, evaluation, incomplete=True,
                    )
                    if messages and messages[-1].get("role") == "assistant":
                        messages.pop()
                    incomplete_text = (
                        "verification_incomplete: Independent verification "
                        "findings were not resolved before the model-call "
                        "budget was exhausted."
                    )
                    messages.append({
                        "role": "assistant",
                        "content": [{"type": "text", "text": incomplete_text}],
                    })
                    record_hook("Stop")
                    trigger_hooks("Stop", messages)
                    finish_run(incomplete_text, status="blocked")
                    return
            unresolved = _incomplete_todos(runtime)
            if unresolved:
                _append_todo_warning(response.content, unresolved)
            record_hook("Stop")
            trigger_hooks("Stop", messages)
            finish_run(extract_text(response.content))
            return
        if not has_tool_use(response.content):
            if background_workers_alive() and CASE_DEADLINE is not None:
                remaining = _remaining_case_time(runtime)
                if not wait_for_background_tasks(remaining):
                    raise _CaseTimeoutError(
                        "eval case deadline exceeded while waiting for background tasks")
            # A worker may finish while the model is producing its final text,
            # so collect completed notifications even when no thread is alive
            # by the time this branch is reached.
            notes = collect_background_results()
            if notes:
                _record_background_notifications(notes, "final_wait")
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": note} for note in notes]})
                continue
            # Interactive CLI runs have no case deadline. Preserve real
            # background semantics: finish this turn immediately and inject
            # the task notification on a later user turn when it is ready.
            if background_workers_alive() and CASE_DEADLINE is None:
                record_hook("Stop")
                trigger_hooks("Stop", messages)
                finish_run(extract_text(response.content))
                return
            remaining = _remaining_case_time(runtime)
            notification_wait = 2.0 if remaining is None else max(0, min(2.0, remaining))
            if wait_for_imminent_once(notification_wait):
                continue
            unresolved_todos = _incomplete_todos(runtime)
            if unresolved_todos:
                if not todo_completion_followup_sent:
                    messages.append({
                        "role": "user",
                        "content": _todo_completion_message(
                            unresolved_todos,
                        ),
                    })
                    todo_completion_followup_sent = True
                    record_event(
                        "todo_completion_reminder",
                        decision="todo_completion_followup",
                        unresolved_count=len(unresolved_todos),
                    )
                    continue
                _append_todo_warning(response.content, unresolved_todos)
                record_event(
                    "todo_completion_reminder", decision="incomplete_final",
                    unresolved_count=len(unresolved_todos),
                )
            if verification_resolution is not None:
                evaluation = _evaluate_verification_resolution(
                    verification_resolution,
                    snapshot_workspace(verification_workdir),
                )
                if evaluation["success"]:
                    _apply_resolution_transitions(
                        verification_resolution, evaluation,
                    )
                    record_hook("Stop")
                    trigger_hooks("Stop", messages)
                    finish_run(extract_text(response.content))
                    return
                if messages and messages[-1].get("role") == "assistant":
                    messages.pop()
                if not verification_resolution.get("reminder_sent"):
                    verification_resolution["reminder_sent"] = True
                    messages.append({
                        "role": "user",
                        "content": _resolution_reminder(evaluation),
                    })
                    record_event(
                        "verification_resolution_reminder",
                        decision="resolution_reminder_sent",
                        workspace_modified=bool(evaluation["code_changed"]),
                        public_suite_passed=bool(
                            evaluation["public_suite_passed"]),
                        targeted_tests_passed=int(
                            evaluation["targeted_tests_passed"]),
                    )
                    continue
                _apply_resolution_transitions(
                    verification_resolution, evaluation, incomplete=True,
                )
                incomplete_text = (
                    "verification_incomplete: Independent verification "
                    "findings remain unresolved after the resolution reminder."
                )
                messages.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": incomplete_text}],
                })
                record_hook("Stop")
                trigger_hooks("Stop", messages)
                finish_run(incomplete_text, status="blocked")
                return
            verification_skip_reason = ""
            if not complexity.get("implementation_task", False):
                verification_skip_reason = "not_implementation_task"
            elif int(complexity.get("score", 0)) < VERIFIER_COMPLEXITY_THRESHOLD:
                verification_skip_reason = "below_complexity_threshold"
            elif verifier_run_limit <= 0:
                verification_skip_reason = "verification_disabled"
            elif verifier_runs >= verifier_run_limit:
                verification_skip_reason = "max_runs_reached"
            else:
                from .subagent import run_independent_verifier
                current_snapshot = snapshot_workspace(verification_workdir)
                task_changed_files = workspace_changes(
                    task_start_snapshot, current_snapshot,
                )
                if not task_changed_files:
                    verification_skip_reason = "workspace_unchanged"
                else:
                    verifier_runs += 1
                    outcome = run_independent_verifier(
                        verification_workdir,
                        runtime,
                        complexity_score=int(complexity.get("score", 0)),
                        observed_tests=_observed_lead_tests(
                            messages, VERIFIER_MAX_TESTS,
                        ),
                        changed_files=task_changed_files,
                    )
                    if outcome.get("invoked") and outcome.get("status") == "pass":
                        record_hook("Stop")
                        trigger_hooks("Stop", messages)
                        finish_run(extract_text(response.content))
                        return
                    # The pending Lead final must not survive a failed gate.
                    if messages and messages[-1].get("role") == "assistant":
                        messages.pop()
                    if outcome.get("invoked"):
                        if outcome.get("status") == "findings":
                            verification_resolution = {
                                "findings": [
                                    dict(item) for item in outcome.get(
                                        "report", {},
                                    ).get("findings", [])
                                    if isinstance(item, dict)
                                ],
                                "verifier_tests": [
                                    dict(item) for item in outcome.get(
                                        "report", {},
                                    ).get("tests_run", [])
                                    if isinstance(item, dict)
                                ],
                                "leader_tests": [],
                                "feedback_snapshot": snapshot_workspace(
                                    verification_workdir,
                                ),
                                "reminder_sent": False,
                            }
                        messages.append({
                            "role": "user",
                            "content": _verification_feedback(outcome),
                        })
                    else:
                        reason = str(outcome.get(
                            "failure_reason", "verification_unavailable",
                        ))
                        messages.append({
                            "role": "user",
                            "content": (
                                "Independent verification was skipped "
                                f"({reason}). Perform focused self-verification "
                                "against the original task and README, rerun the "
                                "public tests, then provide a final answer. The "
                                "verifier will not be retried."
                            ),
                        })
                    continue
            if verification_skip_reason:
                record_event(
                    "verification_skipped",
                    verification_skipped_reason=verification_skip_reason,
                    complexity_score=int(complexity.get("score", 0)),
                    threshold=VERIFIER_COMPLEXITY_THRESHOLD,
                )
            record_hook("Stop")
            trigger_hooks("Stop", messages)
            finish_run(extract_text(response.content))
            return

        results = []
        compacted_now = False
        for block in response.content:
            _check_case_deadline(runtime)
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            record_tool_use(block)

            mutation_requested = block.name in _MAIN_MUTATION_TOOLS
            delegated_role = ""
            if block.name == "delegate_agent":
                delegated_role = normalize_agent_role(
                    block.input.get("role", ""))
            elif block.name == "task":
                delegated_role = "general-purpose"

            if block.name in {"delegate_agent", "task"} \
                    and delegated_role == "explore" \
                    and explorer_attempted:
                cached = _tool_json(explorer_cached_result)
                cached["reused"] = True
                output = json.dumps(cached)
                record_event(
                    "delegation_reused", agent_role="explore",
                    tool_use_id=block.id,
                )
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue
            if (block.name in {"delegate_agent", "task"}
                    and delegated_role == "review"
                    and reviewer_attempted_revision == mutation_revision):
                cached = _tool_json(reviewer_cached_result)
                cached["reused"] = True
                output = json.dumps(cached)
                record_event(
                    "delegation_reused", agent_role="review",
                    tool_use_id=block.id, mutation_revision=mutation_revision,
                )
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue
            if block.name == "compact":
                compact_sizer = _request_sizer(context, tools, runtime)
                messages[:] = (
                    compact_history(
                        messages,
                        runtime=runtime,
                        reason="manual",
                        target_context_budget=_compact_target_chars(),
                        request_size_fn=compact_sizer,
                    )
                    if runtime is not None else compact_history(
                        messages,
                        reason="manual",
                        target_context_budget=_compact_target_chars(),
                        request_size_fn=compact_sizer,
                    )
                )
                output = "[Compacted. Continue with summarized context.]"
                compact_tool_use_preserved = any(
                    candidate.get("role") == "assistant"
                    and any(block_type(item) == "tool_use"
                            and ((item.get("id") if isinstance(item, dict)
                                  else getattr(item, "id", None)) == block.id)
                            for item in candidate.get("content", []))
                    for candidate in messages
                )
                if compact_tool_use_preserved:
                    messages.append({"role": "user", "content": [{
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }]})
                else:
                    messages.append({"role": "user", "content": output})
                record_tool_result(block.id, block.name, output)
                compacted_now = True
                break

            record_hook("PreToolUse", tool=block.name, stage="before")
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                blocked_text = tool_rejection_text(blocked)
                record_hook("PreToolUse", tool=block.name,
                            tool_use_id=block.id, input=block.input,
                            decision="blocked", reason=blocked_text,
                            recoverable=is_recoverable_tool_rejection(blocked))
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": blocked_text})
                record_tool_result(block.id, block.name, blocked_text)
                if is_recoverable_tool_rejection(blocked):
                    continue
                if is_permission_denied_output(blocked_text):
                    stop_after_permission_denied(messages, blocked_text)
                    finish_run(blocked_text)
                    return
                continue
            record_hook("PreToolUse", tool=block.name, decision="allowed")

            resolution_test_command = bool(
                verification_resolution is not None
                and block.name == "bash"
                and is_test_command(str(block.input.get("command", "")))
            )
            if (should_run_background(block.name, block.input)
                    and not resolution_test_command):
                routing_reason = (
                    "explicit" if block.input.get("run_in_background")
                    else "slow_command"
                )
                bg_id = start_background_task(block, handlers)
                record_event(
                    "background_routed",
                    tool=block.name,
                    tool_use_id=block.id,
                    task_id=bg_id,
                    command=block.input.get("command", ""),
                    reason=routing_reason,
                )
                output = (f"[Background task {bg_id} started] "
                          "Result will arrive as a task_notification. Do not "
                          "rerun the same command, poll with check_inbox, or "
                          "launch a task/subagent just to wait; continue "
                          "independent work or finish your turn.")
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                record_tool_result(block.id, block.name, output)
                continue

            handler = handlers.get(block.name)
            handler_input = dict(block.input)
            if resolution_test_command:
                handler_input["_report_exit_code"] = True
            output = call_tool_handler(
                handler,
                handler_input,
                block.name,
                tool_use_id=block.id,
            )
            if resolution_test_command:
                fact = captured_test_fact(
                    str(block.input.get("command", "")),
                    _bash_exit_code(str(output)),
                    len(verification_resolution["leader_tests"]) + 1,
                    id_prefix="leader_test",
                )
                verification_resolution["leader_tests"].append(fact)
                output = (
                    f"{output}\n<harness_test_fact>"
                    f"{json.dumps(fact, ensure_ascii=False)}"
                    "</harness_test_fact>"
                )
            trigger_hooks("PostToolUse", block, output)
            record_hook("PostToolUse", tool=block.name)
            print(str(output)[:300])

            delegated_changed_files: list[str] = []
            if (block.name in {"delegate_agent", "task"}
                    and not str(output).startswith("Error:")):
                delegation = _tool_json(output)
                raw_changed_files = delegation.get("changed_files", [])
                if isinstance(raw_changed_files, list):
                    delegated_changed_files = [
                        str(path).strip() for path in raw_changed_files
                        if str(path).strip()
                    ]
                verdict = str(delegation.get("verdict", "")).lower()
                if delegated_role == "explore":
                    explorer_attempted = True
                    explorer_cached_result = str(output)
                    record_event(
                        "subagent_policy", decision="explore_observed",
                        verdict=verdict or "unknown",
                    )
                elif delegated_role == "review":
                    reviewer_attempted_revision = mutation_revision
                    reviewer_cached_result = str(output)
                    screening = _screen_reviewer_findings(
                        delegation, runtime,
                    )
                    findings = screening["findings"]
                    output = _screened_reviewer_output(
                        delegation, screening,
                    )
                    reviewer_cached_result = str(output)
                    record_event(
                        "subagent_policy",
                        decision=("reviewer_pass" if (
                            delegation.get("status") == "completed"
                            and verdict == "pass" and not findings
                        ) else "reviewer_observed"),
                        verdict=verdict or "unknown",
                        finding_count=len(findings),
                        raw_finding_count=screening["raw_count"],
                        suppressed_finding_count=len(screening["suppressed"]),
                        mutation_revision=mutation_revision,
                    )

            mutation_succeeded = (
                (mutation_requested or bool(delegated_changed_files))
                and not str(output).lower().startswith((
                    "error:", "permission denied", "tool not run"))
            )
            integration = (
                _tool_json(output)
                if block.name == "integrate_worktree" else {}
            )
            if block.name == "integrate_worktree":
                mutation_succeeded = integration.get("status") == "integrated"
            if mutation_succeeded:
                mutation_revision += 1
                changed_path = str(block.input.get("path", "")).strip()
                if changed_path:
                    changed_file_paths.add(changed_path)
                for changed_path in integration.get("changed_files", []):
                    if changed_path:
                        changed_file_paths.add(str(changed_path))
                for changed_path in delegated_changed_files:
                    changed_file_paths.add(changed_path)

            if (block.name == "read_file"
                    and not str(output).lower().startswith("error:")):
                read_path = _os.path.normpath(
                    str(block.input.get("path", "")).strip()
                ).replace("\\", "/").lower()
                if read_path:
                    lead_read_paths.add(read_path)
                    lead_read_counts[read_path] = (
                        lead_read_counts.get(read_path, 0) + 1)
                if (multiagent_enabled and not runtime_benefit_signal_sent
                        and not explorer_attempted
                        and complexity.get("implementation_task", False)):
                    benefit = _runtime_role_benefit(
                        lead_read_counts, model_client)
                    if benefit["evidence_ready"]:
                        runtime_benefit_signal_sent = True
                        event_budget = {
                            key: value for key, value in benefit["budget"].items()
                            if key != "available"
                        }
                        if benefit["eligible"]:
                            record_event(
                                "multiagent_policy",
                                decision="runtime_benefit_observed",
                                unique_read_paths=benefit["unique_read_paths"],
                                repeated_reads=benefit["repeated_reads"],
                                scope_count=benefit["scope_count"],
                                repeated_paths=benefit["repeated_paths"],
                                **event_budget,
                            )
                        else:
                            record_event(
                                "multiagent_policy",
                                decision="runtime_benefit_observed_no_budget",
                                unique_read_paths=benefit["unique_read_paths"],
                                repeated_reads=benefit["repeated_reads"],
                                scope_count=benefit["scope_count"],
                                **event_budget,
                            )

            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
            record_tool_result(block.id, block.name, output)
            if is_permission_denied_output(output):
                stop_after_permission_denied(messages, str(output))
                finish_run(str(output))
                return

        if compacted_now:
            continue

        messages.append({"role": "user", "content": build_user_content(results)})


def print_turn_assistants(messages: list, turn_start: int):
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if block_type(block) == "text":
                terminal_print(block["text"] if isinstance(block, dict) else block.text)


def cron_autorun_loop(
    history: list,
    context: dict,
    runtime: AgentRuntime | None = None,
):
    while True:
        time.sleep(1)
        fired = consume_cron_queue()
        if not fired:
            continue
        with agent_lock:
            turn_start = len(history)
            for job in fired:
                history.append({"role": "user",
                                "content": scheduled_prompt_text(job)})
                prefix = "once auto" if getattr(job, "kind", "") == "once" else "cron auto"
                terminal_print(
                    f"  \033[35m[{prefix}] {job.prompt[:60]}\033[0m")
            scheduled_prompt = "\n".join(
                scheduled_prompt_text(job) for job in fired)
            if runtime is not None:
                runtime.state.root_task = scheduled_prompt
            start_run(scheduled_prompt, workdir=WORKDIR,
                      model_provider=MODEL_PROVIDER, model=MODEL)
            try:
                if runtime is not None:
                    agent_loop(history, context, runtime)
                    context.update(update_context(context, history, runtime))
                else:
                    agent_loop(history, context)
                    context.update(update_context(context, history))
                print_turn_assistants(history, turn_start)
                final_text = ""
                for msg in reversed(history[turn_start:]):
                    if msg.get("role") == "assistant":
                        final_text = extract_text(msg.get("content", ""))
                        break
                finish_run(final_text)
            except Exception as e:
                record_error(e)
                finish_run(f"[Error] {type(e).__name__}: {e}")
                raise


def _set_runtime_value(name: str, value):
    from . import runtime_state as _state

    setattr(_state, name, value)
    for module in getattr(_state, "_REGISTERED_MODULES", []):
        if hasattr(module, name):
            setattr(module, name, value)
    if name == "WORKDIR":
        for module in getattr(_state, "_REGISTERED_MODULES", []):
            prompt_sections = getattr(module, "PROMPT_SECTIONS", None)
            if isinstance(prompt_sections, dict) and "workspace" in prompt_sections:
                prompt_sections["workspace"] = f"Working directory: {value}"


def _runtime_value(name: str):
    from . import runtime_state as _state

    return getattr(_state, name)


_WORKDIR_DERIVED_PATHS = {
    "SKILLS_DIR": ("skills",),
    "TRANSCRIPT_DIR": (".transcripts",),
    "TOOL_RESULTS_DIR": (".task_outputs", "tool-results"),
    "MEMORY_DIR": (".memory",),
    "MEMORY_INDEX": (".memory", "MEMORY.md"),
    "MAILBOX_DIR": (".mailboxes",),
    "TASKS_DIR": (".tasks",),
    "WORKTREES_DIR": (".worktrees",),
    "DURABLE_PATH": (".scheduled_tasks.json",),
    "ONCE_DURABLE_PATH": (".scheduled_once_tasks.json",),
}

_ISOLATED_RUNTIME_COLLECTIONS = (
    "mcp_clients", "active_teammates", "teammate_threads",
    "teammate_stop_events", "pending_requests", "scheduled_jobs",
    "scheduled_once_jobs", "CRON_LAST_FIRED", "background_tasks",
    "background_results",
)
_ISOLATED_RUNTIME_LISTS = ("cron_queue", "CURRENT_TODOS")


def _isolate_runtime_collections() -> dict:
    snapshots = {}
    for name in _ISOLATED_RUNTIME_COLLECTIONS:
        value = _runtime_value(name)
        snapshots[name] = dict(value)
        value.clear()
    for name in _ISOLATED_RUNTIME_LISTS:
        value = _runtime_value(name)
        snapshots[name] = list(value)
        value.clear()
    return snapshots


def _restore_runtime_collections(snapshots: dict):
    for name, snapshot in snapshots.items():
        value = _runtime_value(name)
        value.clear()
        if isinstance(value, dict):
            value.update(snapshot)
        else:
            value.extend(snapshot)


def _set_runtime_workdir(workdir, runtime_root=None):
    _set_runtime_value("WORKDIR", workdir)
    state_root = _Path(runtime_root).resolve() if runtime_root else workdir
    for name, parts in _WORKDIR_DERIVED_PATHS.items():
        _set_runtime_value(name, state_root.joinpath(*parts))


def _copy_trace_file(source, target):
    if not source or not target:
        return
    source_path = _Path(source)
    target_path = _Path(target)
    if source_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(source_path, target_path)


def run_agent_task(task: str, workdir: str, trace_path: str | None = None,
                   *, model_client=None, model_provider: str | None = None,
                   model: str | None = None, command_executor=None,
                   tool_policy: dict | None = None,
                   case_deadline: float | None = None,
                   cleanup_grace: float = 2.0,
                   trace_storage_root: str | None = None,
                   runtime_root: str | None = None,
                   manage_lifecycle: bool = False,
                   approval_mode: str | None = None,
                   context_limit_chars: int | None = None,
                   compact_trigger_ratio: float | None = None) -> dict:
    """Run one non-interactive agent task using the existing loop and trace."""
    from . import bootstrap
    bootstrap()

    workdir_path = _Path(workdir).resolve()
    workdir_path.mkdir(parents=True, exist_ok=True)
    state_names = [
        "WORKDIR", "client", "MODEL_PROVIDER", "MODEL", "PRIMARY_MODEL",
        "COMMAND_EXECUTOR", "TOOL_POLICY", "CASE_DEADLINE",
        "CURRENT_ROOT_TASK", "BACKGROUND_TASKS_ENABLED", "APPROVAL_MODE",
        "AGENT_CONTEXT_LIMIT_TOKENS", "CONTEXT_LIMIT_TOKENS",
        "CONTEXT_LIMIT", "COMPACT_TRIGGER_TOKENS",
        "COMPACT_TRIGGER_RATIO",
        *_WORKDIR_DERIVED_PATHS,
    ]
    old_state = {name: _runtime_value(name) for name in state_names}
    run = None
    runtime = None
    final_text = ""
    cleanup_errors = []
    collection_snapshots = None
    try:
        # The outer try begins before the first runtime mutation.
        _set_runtime_workdir(workdir_path, runtime_root)
        if model_client is not None:
            _set_runtime_value("client", model_client)
        if command_executor is not None:
            _set_runtime_value("COMMAND_EXECUTOR", command_executor)
        _set_runtime_value("TOOL_POLICY", tool_policy)
        _set_runtime_value("CASE_DEADLINE", case_deadline)
        _set_runtime_value("CURRENT_ROOT_TASK", task)
        effective_context_chars = int(_runtime_value("CONTEXT_LIMIT"))
        effective_trigger_ratio = float(
            _runtime_value("COMPACT_TRIGGER_RATIO")
        )
        if context_limit_chars is not None:
            effective_context_chars = int(context_limit_chars)
            if not 16_000 <= effective_context_chars <= 2_000_000:
                raise ValueError(
                    "context_limit_chars must be between 16000 and 2000000")
        if compact_trigger_ratio is not None:
            effective_trigger_ratio = float(compact_trigger_ratio)
            if not 0.25 <= effective_trigger_ratio <= 0.95:
                raise ValueError(
                    "compact_trigger_ratio must be between 0.25 and 0.95")
        if context_limit_chars is not None or compact_trigger_ratio is not None:
            effective_context_tokens = estimate_context_tokens(
                effective_context_chars
            )
            effective_trigger_tokens = max(
                1,
                int(effective_context_tokens * effective_trigger_ratio),
            )
            _set_runtime_value(
                "AGENT_CONTEXT_LIMIT_TOKENS", effective_context_tokens,
            )
            _set_runtime_value(
                "CONTEXT_LIMIT_TOKENS", effective_context_tokens,
            )
            _set_runtime_value("CONTEXT_LIMIT", effective_context_chars)
            _set_runtime_value(
                "COMPACT_TRIGGER_TOKENS", effective_trigger_tokens,
            )
            _set_runtime_value(
                "COMPACT_TRIGGER_RATIO", effective_trigger_ratio,
            )
        if approval_mode is not None:
            if approval_mode not in {"interactive", "non_interactive"}:
                raise ValueError(f"unsupported approval mode: {approval_mode}")
            _set_runtime_value("APPROVAL_MODE", approval_mode)
        if manage_lifecycle:
            collection_snapshots = _isolate_runtime_collections()
        if isinstance(tool_policy, dict) and "background_tasks" in tool_policy:
            _set_runtime_value("BACKGROUND_TASKS_ENABLED", bool(tool_policy["background_tasks"]))

        provider_name = model_provider or _runtime_value("MODEL_PROVIDER")
        model_name = model or _runtime_value("MODEL")
        _set_runtime_value("MODEL_PROVIDER", provider_name)
        _set_runtime_value("MODEL", model_name)
        _set_runtime_value("PRIMARY_MODEL", model_name)
        runtime = AgentRuntime.create(
            workdir=workdir_path,
            state_root=runtime_root,
            model_client=_runtime_value("client"),
            command_executor=_runtime_value("COMMAND_EXECUTOR"),
            model_provider=provider_name,
            model=model_name,
            primary_model=model_name,
            fallback_model=_runtime_value("FALLBACK_MODEL"),
            tool_policy=tool_policy,
            approval_mode=_runtime_value("APPROVAL_MODE"),
            background_tasks_enabled=bool(
                _runtime_value("BACKGROUND_TASKS_ENABLED")),
            root_task=task,
            deadline=case_deadline,
        )
        run = start_run(task, workdir=workdir_path,
                        model_provider=provider_name, model=model_name,
                        storage_root=(_Path(trace_storage_root).resolve()
                                      if trace_storage_root else None))
        record_hook("UserPromptSubmit", input=task)
        trigger_hooks("UserPromptSubmit", task)
        if tool_policy:
            record_event("tool_policy", **tool_policy)
        record_event(
            "context_configuration",
            context_limit_chars=int(_runtime_value("CONTEXT_LIMIT")),
            context_limit_tokens=int(
                _runtime_value("AGENT_CONTEXT_LIMIT_TOKENS")),
            agent_context_limit_tokens=int(
                _runtime_value("AGENT_CONTEXT_LIMIT_TOKENS")),
            compact_trigger_tokens=int(
                _runtime_value("COMPACT_TRIGGER_TOKENS")),
            compact_trigger_ratio=float(
                _runtime_value("COMPACT_TRIGGER_RATIO")),
            summary_input_limit_tokens=int(
                _runtime_value("SUMMARY_INPUT_LIMIT_TOKENS")),
            summary_max_tokens=int(_runtime_value("SUMMARY_MAX_TOKENS")),
            eval_override=bool(
                context_limit_chars is not None
                or compact_trigger_ratio is not None),
        )

        messages = [{"role": "user", "content": task}]
        runtime.services.trace_recorder = run
        context = update_context({}, [], runtime)
        if manage_lifecycle:
            start_scheduler(load_durable=True)
        if command_executor is not None:
            command_executor.start()
        with agent_lock:
            agent_loop(messages, context, runtime)
            update_context(context, messages, runtime)
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                final_text = _message_text(msg.get("content", ""))
                break
        if get_current_run():
            finish_run(final_text)
    except Exception as exc:
        try:
            record_error(exc)
        except Exception:
            pass
        final_text = f"[Error] {type(exc).__name__}: {exc}"
        try:
            finish_run(final_text)
        except Exception:
            pass
        raise
    finally:
        active_exception = _sys.exc_info()[0] is not None
        cleanup_deadline = _time.monotonic() + max(0, cleanup_grace)

        def cleanup_remaining() -> float:
            return max(0, cleanup_deadline - _time.monotonic())

        def cleanup_step(func):
            try:
                func()
            except BaseException as cleanup_exc:
                cleanup_errors.append(cleanup_exc)

        def cleanup_status(func, failure_message: str) -> bool:
            try:
                stopped = bool(func())
            except BaseException as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
                return False
            if not stopped:
                cleanup_errors.append(RuntimeError(failure_message))
            return stopped

        lifecycle_stopped = True
        if manage_lifecycle:
            scheduler_stopped = cleanup_status(
                lambda: stop_scheduler(cleanup_remaining()),
                "scheduler thread did not stop",
            )
            teammates_stopped = cleanup_status(
                lambda: stop_all_teammates(cleanup_remaining()),
                "teammate threads did not stop",
            )
            lifecycle_stopped = scheduler_stopped and teammates_stopped
        # Stop the executor so in-flight command calls unblock, then give
        # background workers only a small bounded grace period.
        if command_executor is not None:
            cleanup_step(command_executor.stop)
        background_stopped = cleanup_status(
            lambda: wait_for_background_tasks(cleanup_remaining()),
            "background worker threads did not stop",
        )
        if run is not None:
            cleanup_step(lambda: _copy_trace_file(run.trace_path, trace_path))
        # Restoring these dicts while an owned worker still references them is
        # a race. A one-shot eval process will fail closed instead.
        if collection_snapshots is not None and lifecycle_stopped and background_stopped:
            cleanup_step(lambda: _restore_runtime_collections(collection_snapshots))
        for name in reversed(state_names):
            cleanup_step(lambda name=name: _set_runtime_value(name, old_state[name]))
        if cleanup_errors and not active_exception:
            raise cleanup_errors[0]

    return {
        "run_id": run.run_id,
        "run_dir": str(run.run_dir),
        "trace_path": str(_Path(trace_path).resolve()) if trace_path else str(run.trace_path),
        "timeline_path": str(run.timeline_path),
        "final_path": str(run.final_path),
        "final_answer": final_text,
        "execution": (command_executor or old_state["COMMAND_EXECUTOR"]).execution_metadata(),
    }



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
