from .runtime_state import *
from .agent_profiles import get_agent_profile, normalize_agent_role
from .model_budget import can_spend_optional_calls, model_budget_snapshot
from .runtime import AgentRuntime
from .model_api import assistant_message_from_response
from .tool_registry import (
    delegated_policy_for_role,
    effective_tool_names,
)
import os as _os
import time as _time
import hashlib as _hashlib
from .command_executor import CaseTimeoutError as _CaseTimeoutError


_SNAPSHOT_EXCLUDED_DIRS = frozenset({
    ".git", ".aqours_code", ".transcripts", ".task_outputs", ".tasks",
    ".mailboxes", ".worktrees", ".pytest_cache", "__pycache__",
    ".mypy_cache", ".ruff_cache", "node_modules",
})
_SNAPSHOT_EXCLUDED_FILES = frozenset({".coverage"})


def snapshot_workspace(workdir: str | Path) -> dict[str, str]:
    """Fingerprint material workspace files for mutation detection."""
    root = Path(workdir).resolve()
    fingerprints: dict[str, str] = {}
    if not root.is_dir():
        return fingerprints
    for directory, dirnames, filenames in _os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if name not in _SNAPSHOT_EXCLUDED_DIRS
            and not (Path(directory) / name).is_symlink()
        ]
        for filename in filenames:
            candidate = Path(directory) / filename
            try:
                if (filename in _SNAPSHOT_EXCLUDED_FILES
                        or filename.startswith(".coverage.")):
                    continue
                resolved = candidate.resolve()
                if not candidate.is_file() or not resolved.is_relative_to(root):
                    continue
                relative = resolved.relative_to(root)
                fingerprints[relative.as_posix()] = _hashlib.sha256(
                    candidate.read_bytes(),
                ).hexdigest()
            except (OSError, ValueError):
                continue
    return fingerprints


# Compatibility for callers that used the previous private helper.
_snapshot_workspace = snapshot_workspace


def workspace_changes(
    before: dict[str, str], after: dict[str, str],
) -> list[str]:
    return sorted(
        path for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )

# ── Subagent Tool ──

def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        elif getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "\n".join(parts).strip()


def has_tool_use(content) -> bool:
    # Do not rely on stop_reason alone; the concrete tool_use block is the
    # continuation signal used by the loop.
    return any((block.get("type") if isinstance(block, dict)
                else getattr(block, "type", None)) == "tool_use"
               for block in content)


def _block_value(block, name: str, default=None):
    return block.get(name, default) if isinstance(block, dict) else getattr(block, name, default)


def _request_with_deadline(*, system: str, messages: list, tools: list,
                           purpose: str, role: str, max_tokens: int = 8000,
                           runtime: AgentRuntime | None = None):
    deadline = runtime.state.deadline if runtime is not None else CASE_DEADLINE
    remaining = None if deadline is None else deadline - _time.monotonic()
    if remaining is not None and remaining <= 0:
        raise _CaseTimeoutError("eval case deadline exceeded")
    old_timeout = _os.environ.get("AQOURS_CODE_REQUEST_TIMEOUT")
    if remaining is not None:
        try:
            configured = float(old_timeout or "30")
        except (TypeError, ValueError):
            configured = 30.0
        _os.environ["AQOURS_CODE_REQUEST_TIMEOUT"] = str(max(0.1, min(configured, remaining)))
    model = runtime.config.model if runtime is not None else MODEL
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    record_llm_request(
        model=model, max_tokens=max_tokens, message_count=len(messages),
        tool_count=len(tools), purpose=purpose, agent_role=role,
    )
    try:
        response = model_client.messages.create(
            model=model, system=system, messages=messages,
            tools=tools, max_tokens=max_tokens,
        )
        record_llm_response(response, purpose=purpose, agent_role=role)
        return response
    finally:
        if remaining is not None:
            if old_timeout is None:
                _os.environ.pop("AQOURS_CODE_REQUEST_TIMEOUT", None)
            else:
                _os.environ["AQOURS_CODE_REQUEST_TIMEOUT"] = old_timeout


def _role_handlers(
    cwd: Path,
    tool_names,
    role: str,
    runtime: AgentRuntime | None = None,
) -> dict:
    pinned_executor = (
        runtime.services.command_executor if runtime is not None
        else COMMAND_EXECUTOR
    )
    return tool_handlers_for_names(
        tool_names,
        role=role,
        runtime=runtime,
        cwd=cwd,
        executor=pinned_executor,
    )


def _parse_role_result(text: str, role: str) -> dict:
    role = normalize_agent_role(role)
    raw = str(text or "").strip()
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S | re.I)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            if role == "verifier":
                return _normalize_verifier_result(value)
            value.setdefault("verdict", "blocked")
            value.setdefault("summary", "")
            return (_normalize_reviewer_result(value)
                    if role == "review" else value)
    fallback = "blocked"
    if role == "review":
        return _fallback_reviewer_result(raw)
    if role == "verifier":
        return {
            "status": "inconclusive",
            "summary": _short_text(raw, 700),
            "tests_run": [],
            "findings": [],
            "invalid_json": True,
            "failure_reason": "invalid_verifier_json",
        }
    return {"verdict": fallback, "summary": raw[:4000], "invalid_json": True}


def _short_text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalize_verifier_result(value: dict) -> dict:
    status = _short_text(value.get("status", "inconclusive"), 20).lower()
    if status == "blockers":
        status = "findings"
    if status not in {"pass", "findings", "inconclusive"}:
        status = "inconclusive"
    raw_findings = value.get("findings", value.get("blockers", []))
    if not isinstance(raw_findings, list):
        raw_findings = []
    findings = []
    for raw in raw_findings[:8]:
        if not isinstance(raw, dict):
            continue
        findings.append({
            "requirement": _short_text(raw.get("requirement", ""), 300),
            "location": _short_text(raw.get("location", ""), 240),
            "expected": _short_text(raw.get("expected", ""), 500),
            "observed": _short_text(raw.get("observed", ""), 500),
            "evidence": _short_text(raw.get("evidence", ""), 700),
        })
    if findings and status == "pass":
        status = "findings"
    return {
        "status": status,
        "summary": _short_text(value.get("summary", ""), 700),
        # Never trust model-supplied test claims. Finalization replaces this
        # with facts captured from actual verifier Bash executions.
        "tests_run": [],
        "findings": findings,
    }


_VERIFIER_TEST_MARKERS = (
    "pytest", "unittest", "python -m test", "python3 -m test", "tox",
    "nox", "npm test", "npm run test", "pnpm test", "yarn test",
    "cargo test", "go test", "dotnet test", "mvn test", "gradle test",
    "./gradlew test",
)
_VERIFIER_MUTATION_PATTERN = re.compile(
    r"(?i)(?:^|[;&|]\s*)(?:rm|mv|cp|touch|mkdir|rmdir|truncate|tee)\b|"
    r"\bsed\s+-[^\n;]*i\b|\bperl\s+-[^\n;]*i\b|"
    r"\bgit\s+(?:add|commit|checkout|switch|restore|reset|clean|apply)\b|"
    r"\b(?:pip|pip3)\s+install\b|\b(?:npm|pnpm|yarn)\s+install\b"
)


def _is_verifier_test_command(command: str) -> bool:
    normalized = " ".join(str(command or "").lower().split())
    return (
        any(marker in normalized for marker in _VERIFIER_TEST_MARKERS)
        or ("assert " in normalized and (
            "python" in normalized or "<<" in normalized or "/tmp/" in normalized
        ))
    )


def _verifier_bash_rejection(command: str) -> str:
    value = str(command or "").strip()
    if not value:
        return "empty_command"
    if _VERIFIER_MUTATION_PATTERN.search(value):
        return "workspace_mutation_command"
    # Output redirection is permitted only for disposable /tmp artifacts or
    # here-doc counterexamples. File-descriptor redirects such as 2>&1 are safe.
    stripped_fds = re.sub(r"\d*>&\d+", "", value)
    if re.search(r"(?:^|\s)(?:>|>>)(?![&])", stripped_fds):
        if "/tmp/" not in value.replace("\\", "/") and "<<" not in value:
            return "workspace_redirection"
    return ""


def _bash_exit_code(output: str) -> int | None:
    match = re.match(r"\[exit_code=(-?\d+)\]", str(output or ""))
    return int(match.group(1)) if match else None


def _normalize_reviewer_result(value: dict) -> dict:
    findings = []
    raw_findings = value.get("findings", [])
    if not isinstance(raw_findings, list):
        raw_findings = [raw_findings] if raw_findings else []
    for raw in raw_findings[:5]:
        if isinstance(raw, dict):
            finding = {
                "severity": _short_text(raw.get("severity", "warning"), 20),
                "requirement": _short_text(
                    raw.get("requirement") or raw.get("contract_clause")
                    or raw.get("title") or "Reviewer finding", 220),
                "file": _short_text(raw.get("file", ""), 240),
                "symbol": _short_text(raw.get("symbol", ""), 120),
                "evidence": _short_text(
                    raw.get("evidence") or raw.get("detail")
                    or raw.get("message"), 500),
            }
        else:
            finding = {
                "severity": "warning",
                "requirement": "Reviewer finding",
                "file": "",
                "symbol": "",
                "evidence": _short_text(raw, 500),
            }
        if finding["evidence"] or finding["requirement"] != "Reviewer finding":
            findings.append(finding)
    verdict = _short_text(value.get("verdict", "blocked"), 20).lower()
    if verdict not in {"pass", "gaps", "blocked"}:
        verdict = "gaps" if findings else "blocked"
    if findings and verdict == "pass":
        verdict = "gaps"
    files_checked = value.get("files_checked", [])
    if not isinstance(files_checked, list):
        files_checked = [files_checked] if files_checked else []
    missing = value.get("missing_evidence", [])
    if not isinstance(missing, list):
        missing = [missing] if missing else []
    return {
        "verdict": verdict,
        "summary": _short_text(value.get("summary", ""), 500),
        "findings": findings,
        "files_checked": [_short_text(item, 240) for item in files_checked[:16]],
        "missing_evidence": [_short_text(item, 300) for item in missing[:8]],
    }


def _fallback_reviewer_result(raw: str) -> dict:
    text = str(raw or "").strip()
    marker = re.search(
        r"(?is)(critical\s+issue|finding|defect|\bbug\b|incorrect|"
        r"missing|must\s+not|wrong|allows?)\s*[:\-]?\s*(.{0,900})",
        text,
    )
    evidence = _short_text(marker.group(0) if marker else text, 700)
    files = re.findall(r"[A-Za-z0-9_./\\-]+\.py", evidence)
    findings = []
    if marker:
        findings.append({
            "severity": "warning",
            "requirement": "Unparsed reviewer concern requires lead verification",
            "file": _short_text(files[0], 240) if files else "",
            "symbol": "",
            "evidence": evidence,
        })
    return {
        "verdict": "gaps" if marker else "blocked",
        "summary": _short_text(text, 700),
        "findings": findings,
        "files_checked": [],
        "missing_evidence": ["Valid structured reviewer result"],
        "invalid_json": True,
    }


def _bounded_repository_manifest(cwd: Path, max_entries: int = 200) -> str:
    raw = str(run_glob("**/*", cwd=cwd))
    if raw.startswith("Error:"):
        return "(manifest unavailable)"
    entries = [line for line in raw.splitlines() if line.strip()]
    selected = entries[:max_entries]
    suffix = (
        f"\n... [{len(entries) - max_entries} more entries omitted]"
        if len(entries) > max_entries else ""
    )
    return "\n".join(selected) + suffix if selected else "(no files found)"


def _successful_tool_output(output: str) -> bool:
    return not str(output).lower().startswith((
        "error:", "permission denied", "tool not run:", "unknown:",
    ))


def _finalize_role_result(
    result: dict, profile, successful_read_paths: set[str],
    verifier_stats: dict | None = None,
) -> dict:
    if profile.name == "verifier":
        normalized = _normalize_verifier_result(result)
        stats = verifier_stats or {}
        actual_tests = list(stats.get("tests_run", []))[:VERIFIER_MAX_TESTS]
        normalized["tests_run"] = actual_tests
        failure_reason = str(result.get("failure_reason", ""))
        if normalized["status"] == "findings" and not normalized["findings"]:
            normalized["status"] = "inconclusive"
            failure_reason = "findings_report_missing_findings"
        elif normalized["status"] == "pass" and not actual_tests:
            normalized["status"] = "inconclusive"
            failure_reason = "no_actual_bash_test"
        elif (normalized["status"] == "pass"
              and any(test.get("result") != "pass" for test in actual_tests)):
            normalized["status"] = "inconclusive"
            failure_reason = "bash_test_failed"
        if stats.get("tool_failure"):
            if normalized["status"] == "pass":
                normalized["status"] = "inconclusive"
            failure_reason = failure_reason or str(stats["tool_failure"])
        if failure_reason:
            normalized["failure_reason"] = failure_reason
        normalized["_verification_stats"] = {
            "model_calls": int(stats.get("model_calls", 0)),
            "tool_calls": int(stats.get("tool_calls", 0)),
            "tests_run": actual_tests,
        }
        return normalized
    if profile.name not in {"explore", "plan", "review"}:
        return result
    normalized = dict(result)
    normalized["files_checked"] = sorted(successful_read_paths)[
        :profile.max_read_paths]
    if profile.name == "explore":
        source_paths = [
            path for path in successful_read_paths
            if Path(path).suffix.lower() in {
                ".py", ".js", ".ts", ".tsx", ".java", ".kt", ".go", ".rs",
                ".rb", ".php", ".cs", ".cpp", ".c", ".h",
            }
        ]
        if not source_paths:
            normalized["verdict"] = "blocked"
            summary = str(normalized.get("summary", "")).strip()
            normalized["summary"] = (
                summary + " No source file was successfully read; the map is "
                "contract-only and must not be treated as verified code evidence."
            ).strip()[:700]
    return normalized


def run_role_agent(
    role: str,
    prompt: str,
    cwd: Path,
    runtime: AgentRuntime | None = None,
    *,
    max_model_calls: int | None = None,
) -> dict:
    profile = get_agent_profile(role)
    if profile is None:
        return {"verdict": "blocked", "summary": f"unknown role: {role}"}
    role_model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    role_runtime = (
        runtime.child(workdir=cwd, root_task=runtime.state.root_task)
        if runtime is not None else None
    )
    parent_policy = (
        runtime.config.tool_policy if runtime is not None else None
    )
    environment_policy = (
        TOOL_POLICY if isinstance(TOOL_POLICY, dict) else None
    )
    delegated_policy = delegated_policy_for_role(
        parent_policy, profile.name,
    )
    effective_names = effective_tool_names(
        TOOL_REGISTRY,
        profile.tool_names,
        role=profile.name,
        parent_policy=parent_policy,
        environment_policy=environment_policy,
        delegated_policy=delegated_policy,
    )
    tools = tool_schemas_for_names(effective_names, role=profile.name)
    handlers = _role_handlers(
        cwd, effective_names, profile.name, role_runtime,
    )
    runtime_policy = (
        role_runtime.config.tool_policy if role_runtime is not None else None
    )
    policy = (runtime_policy if isinstance(runtime_policy, dict)
              else TOOL_POLICY if isinstance(TOOL_POLICY, dict) else {})
    prompt_runtime = resolve_prompt_runtime_context(policy, cwd)
    root_task = str(
        role_runtime.state.root_task if role_runtime is not None
        else CURRENT_ROOT_TASK or ""
    ).strip() or "(not available)"
    manifest_section = ""
    if profile.name == "explore":
        manifest = _bounded_repository_manifest(cwd)
        manifest_section = (
            "\nHarness-provided repository manifest (do not run glob to "
            f"rediscover it):\n<repository_manifest>\n{manifest}\n"
            "</repository_manifest>\n"
        )
        record_event(
            "delegation_manifest", agent_role=profile.name,
            entry_count=len([
                line for line in manifest.splitlines()
                if line and not line.startswith("... [")
            ]),
            truncated="more entries omitted" in manifest,
        )
    system = (
        f"You are the {profile.name} role in a lead-managed coding task.\n"
        f"{profile.instructions}\n\n"
        f"{format_runtime_context_for_prompt(prompt_runtime)}\n"
        f"Assigned workspace: {prompt_runtime['workdir']}\n"
        f"{manifest_section}"
        "The original root task is authoritative:\n"
        f"<root_task>\n{root_task}\n</root_task>"
    )
    messages = [{"role": "user", "content": prompt}]
    final_text = ""
    tool_rounds = 0
    needs_synthesis = False
    role_evidence: list[str] = []
    read_paths: set[str] = set()
    successful_read_paths: set[str] = set()
    read_cache: set[tuple[str, object, object]] = set()
    executed_tool_calls = 0
    verifier_stats = {
        "model_calls": 0,
        "tool_calls": 0,
        "tests_run": [],
        "tool_failure": "",
    }
    configured_role_calls = (
        profile.max_tool_rounds + 1
        if profile.max_tool_rounds is not None else None
    )
    model_call_limit = (
        None
        if profile.name == "verifier"
        else configured_role_calls
        if max_model_calls is None
        else max(0, int(max_model_calls))
    )
    tool_round_limit = (
        None
        if model_call_limit is None
        else min(
            int(profile.max_tool_rounds or 0),
            max(0, model_call_limit - 1),
        )
    )

    def shared_global_budget_stop() -> dict | None:
        if profile.name != "verifier" or model_call_limit is not None:
            return None
        budget = model_budget_snapshot(role_model_client)
        if (not budget.get("available")
                or int(budget.get("remaining_calls", 0)) > 0):
            return None
        verifier_stats["tool_failure"] = "global_model_call_limit_reached"
        record_event(
            "delegated_model_budget",
            agent_role="verifier",
            decision="global_model_call_limit_reached",
            budget_mode="shared_global",
            global_calls_remaining=0,
        )
        return _finalize_role_result(
            {
                "status": "inconclusive",
                "summary": (
                    "Verifier stopped because the global model-call budget "
                    "was exhausted."
                ),
                "tests_run": [],
                "findings": [],
                "failure_reason": "global_model_call_limit_reached",
            },
            profile,
            successful_read_paths,
            verifier_stats,
        )

    while tool_round_limit is None or tool_rounds < tool_round_limit:
        budget_stop = shared_global_budget_stop()
        if budget_stop is not None:
            return budget_stop
        verifier_stats["model_calls"] += 1
        response = _request_with_deadline(
            system=system, messages=messages, tools=tools,
            purpose="subagent", role=profile.name,
            max_tokens=profile.max_response_tokens,
            runtime=role_runtime,
        )
        messages.append(assistant_message_from_response(response))
        text = extract_text(response.content)
        if text:
            final_text = text
        if not has_tool_use(response.content):
            parsed = _parse_role_result(final_text, profile.name)
            if not parsed.get("invalid_json"):
                return _finalize_role_result(
                    parsed, profile, successful_read_paths, verifier_stats)
            needs_synthesis = True
            break
        tool_rounds += 1
        results = []
        for block in response.content:
            if _block_value(block, "type") != "tool_use":
                continue
            block_name = _block_value(block, "name", "")
            block_id = _block_value(block, "id", "")
            block_input = _block_value(block, "input", {}) or {}
            handler = handlers.get(block_name)
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                output = (tool_rejection_text(blocked)
                          if "tool_rejection_text" in globals() else str(blocked))
            elif (profile.max_tool_calls is not None
                  and executed_tool_calls >= profile.max_tool_calls):
                output = (
                    "Tool not run: this delegation reached its "
                    f"{profile.max_tool_calls}-call execution budget. Synthesize "
                    "from retained evidence or report what is missing."
                )
                record_event(
                    "delegated_tool_budget", agent_role=profile.name,
                    decision="tool_limit_reached",
                    tool_limit=profile.max_tool_calls,
                )
            elif (profile.name == "verifier" and block_name == "bash"
                  and _verifier_bash_rejection(
                      str(block_input.get("command", "")))):
                reason = _verifier_bash_rejection(
                    str(block_input.get("command", "")))
                output = (
                    "Tool not run: verifier bash is read-only; rejected "
                    f"command ({reason})."
                )
                verifier_stats["tool_failure"] = reason
            elif (profile.name == "verifier" and block_name == "bash"
                  and _is_verifier_test_command(
                      str(block_input.get("command", "")))
                  and len(verifier_stats["tests_run"]) >= VERIFIER_MAX_TESTS):
                output = (
                    "Tool not run: verifier reached its "
                    f"{VERIFIER_MAX_TESTS}-test budget."
                )
                verifier_stats["tool_failure"] = "verifier_test_limit_reached"
            elif block_name == "read_file":
                read_path = _os.path.normpath(
                    str(block_input.get("path", "")).strip()
                ).replace("\\", "/").lower()
                read_key = (
                    read_path,
                    block_input.get("offset", 0),
                    block_input.get("limit"),
                )
                if read_key in read_cache:
                    output = (
                        "Tool not run: this exact file range was already returned "
                        "in this delegation. Reuse the retained evidence."
                    )
                    record_event(
                        "delegated_read_reused", agent_role=profile.name,
                        path=read_path,
                    )
                elif (profile.max_read_paths is not None
                      and read_path not in read_paths
                      and len(read_paths) >= profile.max_read_paths):
                    output = (
                        "Tool not run: this delegation reached its "
                        f"{profile.max_read_paths}-path read budget. Synthesize "
                        "from retained evidence or report what is missing."
                    )
                    record_event(
                        "delegated_read_budget", agent_role=profile.name,
                        decision="path_limit_reached",
                        path_limit=profile.max_read_paths,
                    )
                else:
                    output = call_tool_handler(
                        handler, block_input, block_name,
                        tool_use_id=block_id,
                    )
                    executed_tool_calls += 1
                    if profile.name == "verifier":
                        verifier_stats["tool_calls"] += 1
                    read_paths.add(read_path)
                    read_cache.add(read_key)
                    if _successful_tool_output(str(output)):
                        successful_read_paths.add(
                            str(block_input.get("path", "")).strip())
                    trigger_hooks("PostToolUse", block, output)
            else:
                handler_input = dict(block_input)
                if profile.name == "verifier" and block_name == "bash":
                    handler_input["_report_exit_code"] = True
                    try:
                        handler_input["timeout"] = min(
                            max(float(handler_input.get("timeout", 120)), 0.1),
                            120.0,
                        )
                    except (TypeError, ValueError):
                        handler_input["timeout"] = 120.0
                output = call_tool_handler(
                    handler, handler_input, block_name,
                    tool_use_id=block_id,
                )
                executed_tool_calls += 1
                if profile.name == "verifier":
                    verifier_stats["tool_calls"] += 1
                    exit_code = (
                        _bash_exit_code(str(output))
                        if block_name == "bash" else None
                    )
                    if block_name == "bash" and exit_code not in {None, 0}:
                        verifier_stats["tool_failure"] = (
                            "verifier_bash_nonzero_exit"
                        )
                    if block_name == "bash" and _is_verifier_test_command(
                        str(block_input.get("command", "")),
                    ):
                        verifier_stats["tests_run"].append({
                            "command": _short_text(
                                block_input.get("command", ""), 500),
                            "result": "pass" if exit_code == 0 else "fail",
                        })
                        if exit_code is None:
                            verifier_stats["tool_failure"] = (
                                "verifier_test_result_unavailable"
                            )
                    if not _successful_tool_output(str(output)):
                        verifier_stats["tool_failure"] = (
                            "verifier_tool_failure"
                        )
                trigger_hooks("PostToolUse", block, output)
            output_text = str(output)
            if (profile.name == "verifier"
                    and not _successful_tool_output(output_text)):
                verifier_stats["tool_failure"] = (
                    verifier_stats["tool_failure"] or "verifier_tool_failure"
                )
            if profile.name in {"explore", "plan", "review", "verifier"} \
                    and (block_name == "read_file" or profile.name == "verifier") \
                    and _successful_tool_output(output_text):
                evidence_path = str(block_input.get("path", ""))
                role_evidence.append(
                    f"<file path={json.dumps(evidence_path)}>\n"
                    f"{output_text[:4500]}\n</file>"
                )
            preview = output_text[:2000]
            if len(output_text) > 2000:
                preview += f"\n... [truncated {len(output_text) - 2000} chars]"
            record_event(
                "delegated_tool_use", agent_role=profile.name,
                tool=block_name, tool_use_id=block_id, input=block_input,
            )
            record_event(
                "delegated_tool_result", agent_role=profile.name,
                tool=block_name, tool_use_id=block_id,
                result_size=len(output_text), content=preview,
                truncated=len(output_text) > 2000,
            )
            results.append({
                "type": "tool_result", "tool_use_id": block_id,
                "content": str(output),
            })
        messages.append({"role": "user", "content": results})
    else:
        needs_synthesis = True

    if (needs_synthesis
            and model_call_limit is not None
            and verifier_stats["model_calls"] >= model_call_limit):
        return _finalize_role_result(
            _parse_role_result(final_text, profile.name),
            profile,
            successful_read_paths,
            verifier_stats,
        )

    if needs_synthesis:
        budget_stop = shared_global_budget_stop()
        if budget_stop is not None:
            return budget_stop
        if profile.name == "verifier":
            synthesis_instruction = (
                '<synthesis>Tool use is over. Return one compact JSON object and '
                'nothing else: {"status":"pass|findings|inconclusive",'
                '"summary":"max 500 chars","tests_run":[],"findings":'
                '[{"requirement":"","location":"","expected":"",'
                '"observed":"","evidence":""}]}. Treat findings as advisory '
                'review suggestions. Report pass only after the repository\'s '
                'complete public test suite succeeds; if it cannot be identified '
                'or run, report inconclusive. Do not request more tools, edit '
                'files, use Markdown, or narrate reasoning.</synthesis>'
            )
            synthesis_max_tokens = profile.max_response_tokens
        elif profile.name == "review":
            synthesis_instruction = (
                '<synthesis>Tool use is over. Return one compact JSON object and '
                'nothing else: {"verdict":"pass|gaps|blocked","summary":"max '
                '240 chars","findings":[{"severity":"critical|major|minor",'
                '"requirement":"max 220 chars","file":"path","symbol":"name",'
                '"evidence":"max 240 chars"}],"files_checked":[],'
                '"missing_evidence":[]}. Include at '
                'most 3 highest-severity findings. Reason silently. Do not put '
                'withdrawn, retracted, satisfied, safe, or "no defect" items in '
                'findings. Put only actionable concerns in findings; do not '
                'narrate reasoning, use Markdown, or request more tools. A pass '
                'requires zero findings.'
                '</synthesis>'
            )
            # Thinking-capable providers may spend the first few thousand output
            # tokens on hidden reasoning. Leave room for the required JSON instead
            # of turning a useful review into an invalid empty result.
            synthesis_max_tokens = 5000
        elif profile.name == "explore":
            synthesis_instruction = (
                '<synthesis>Tool use is over. Return one compact JSON object and '
                'nothing else: {"verdict":"complete|blocked","summary":"max '
                '300 chars","requirements":["max 12, each max 220 chars"],'
                '"code_map":["max 12 verified path:symbol facts, each max 260 '
                'chars"],"risks":["max 8 verified or explicitly labeled '
                'assumptions, each max 260 chars"],"files_checked":[]}. Prioritize '
                'every distinct contract clause and its concrete implementation '
                'location. Do not include proposed rewrites, Markdown, nested '
                'objects, test-reading requests, or reasoning. Use blocked only '
                'when the gathered source cannot support a useful map.'
                '</synthesis>'
            )
            synthesis_max_tokens = 1600
        else:
            synthesis_instruction = (
                "<synthesis>The tool phase is over. Do not request or describe "
                "more tool calls. Using only the evidence already gathered, "
                "return the role's required JSON object now. Return JSON only. "
                "If evidence is insufficient, use the role's blocked/gaps "
                "verdict and state exactly what is missing.</synthesis>"
            )
            synthesis_max_tokens = 3000
        messages.append({
            "role": "user",
            "content": synthesis_instruction,
        })
        record_event(
            "delegation_synthesis", agent_role=profile.name,
            tool_rounds=tool_rounds,
        )
        synthesis_messages = messages
        if profile.name in {"explore", "plan", "review", "verifier"}:
            # A fresh turn prevents the synthesis model from continuing the
            # role's last unfinished intent (for example, "now run tests")
            # instead of analyzing evidence and returning the required JSON.
            evidence = "\n".join(role_evidence)[:60000]
            synthesis_messages = [{
                "role": "user",
                "content": (
                    f"{synthesis_instruction}\n"
                    f"<assignment>{str(prompt)[:6000]}</assignment>\n"
                    f"<role_evidence>{evidence}</role_evidence>"
                ),
            }]
        verifier_stats["model_calls"] += 1
        response = _request_with_deadline(
            system=system, messages=synthesis_messages, tools=[],
            purpose="subagent", role=profile.name,
            max_tokens=synthesis_max_tokens,
            runtime=role_runtime,
        )
        messages.append(assistant_message_from_response(response))
        final_text = extract_text(response.content)
    return _finalize_role_result(
        _parse_role_result(final_text, profile.name),
        profile,
        successful_read_paths,
        verifier_stats,
    )


def run_independent_verifier(
    cwd: Path,
    runtime: AgentRuntime | None,
    *,
    complexity_score: int,
    observed_tests: list[dict] | None = None,
    changed_files: list[str] | None = None,
) -> dict:
    """Run the one-shot harness verifier with fresh role context."""
    profile = get_agent_profile("verifier")
    if profile is None:
        return {
            "invoked": False,
            "status": "inconclusive",
            "failure_reason": "verifier_profile_unavailable",
        }
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    budget = model_budget_snapshot(model_client)
    global_calls_remaining_at_start = (
        int(budget["remaining_calls"])
        if budget.get("available") else None
    )
    if (global_calls_remaining_at_start is not None
            and global_calls_remaining_at_start <= 0):
        record_event(
            "verification_skipped",
            verification_skipped_reason="global_model_call_limit_reached",
            complexity_score=int(complexity_score),
            threshold=VERIFIER_COMPLEXITY_THRESHOLD,
            budget_mode="shared_global",
            global_calls_remaining_at_start=global_calls_remaining_at_start,
            local_model_call_limit=None,
            local_tool_call_limit=None,
            **{key: value for key, value in budget.items()
               if key != "available"},
        )
        return {
            "invoked": False,
            "status": "inconclusive",
            "failure_reason": "global_model_call_limit_reached",
            "budget_mode": "shared_global",
            "global_calls_remaining_at_start": global_calls_remaining_at_start,
            "local_model_call_limit": None,
            "local_tool_call_limit": None,
        }

    workdir = Path(cwd).resolve()
    before = snapshot_workspace(workdir)
    record_event(
        "verification_start",
        complexity_score=int(complexity_score),
        threshold=VERIFIER_COMPLEXITY_THRESHOLD,
        budget_mode="shared_global",
        global_calls_remaining_at_start=global_calls_remaining_at_start,
        local_model_call_limit=None,
        local_tool_call_limit=None,
        max_tests=VERIFIER_MAX_TESTS,
    )
    observed = []
    for item in (observed_tests or [])[:VERIFIER_MAX_TESTS]:
        if not isinstance(item, dict):
            continue
        observed.append({
            "command": _short_text(item.get("command", ""), 500),
            "result": _short_text(item.get("result", "unknown"), 120),
        })
    assignment = (
        "Independently verify the current final workspace against the original "
        "task. Re-read repository guidance and README files. "
        + (
            "Inspect git status and the complete final diff. "
            if (workdir / ".git").exists()
            else (
                "Git metadata is unavailable in this isolated workspace; do not "
                "run git commands. Inspect these harness-observed changed paths "
                f"and their current dependencies: {json.dumps((changed_files or [])[:50])}. "
            )
        )
        + "Run no more than "
        f"{VERIFIER_MAX_TESTS} high-value tests. Do not modify the workspace.\n"
        "A pass should only be reported after the repository's complete public "
        "test suite succeeds. Targeted tests do not substitute for it; return "
        "inconclusive when the complete suite cannot be identified or run.\n"
        "Observed public test commands and short results, if any:\n"
        f"{json.dumps(observed, ensure_ascii=False)}"
    )
    try:
        raw_result = run_role_agent(
            "verifier", assignment, workdir, runtime,
        )
    except Exception as exc:
        budget_after_error = model_budget_snapshot(model_client)
        global_budget_exhausted = bool(
            budget_after_error.get("available")
            and int(budget_after_error.get("remaining_calls", 0)) <= 0
        )
        raw_result = {
            "status": "inconclusive",
            "summary": "Independent verification could not complete.",
            "tests_run": [],
            "findings": [],
            "failure_reason": (
                "global_model_call_limit_reached"
                if global_budget_exhausted
                else "verifier_timeout" if isinstance(exc, _CaseTimeoutError)
                else f"verifier_error:{type(exc).__name__}"
            ),
            "_verification_stats": {
                "model_calls": 0, "tool_calls": 0, "tests_run": [],
            },
        }
    after = snapshot_workspace(workdir)
    changed_files = workspace_changes(before, after)
    stats = raw_result.pop("_verification_stats", {})
    result = _normalize_verifier_result(raw_result)
    failure_reason = str(raw_result.get("failure_reason", ""))
    if changed_files:
        result["status"] = "inconclusive"
        result["summary"] = (
            "Verifier changed the workspace; its result is invalid."
        )
        failure_reason = "verifier_modified_workspace"
    if result["status"] == "inconclusive" and not failure_reason:
        failure_reason = "verifier_inconclusive"
    model_calls = int(stats.get("model_calls", 0))
    tool_calls = int(stats.get("tool_calls", 0))
    tests_run = list(stats.get("tests_run", result.get("tests_run", [])))
    result["tests_run"] = tests_run[:VERIFIER_MAX_TESTS]
    record_event(
        "verification_result",
        status=result["status"],
        model_calls=model_calls,
        tool_calls=tool_calls,
        tests_run=len(result["tests_run"]),
        findings_found=len(result["findings"]),
        blockers_found=len(result["findings"]),
        budget_mode="shared_global",
        global_calls_remaining_at_start=global_calls_remaining_at_start,
        local_model_call_limit=None,
        local_tool_call_limit=None,
        workspace_modified=bool(changed_files),
        workspace_changed_files=changed_files[:20],
        failure_reason=failure_reason,
    )
    return {
        "invoked": True,
        "status": result["status"],
        "report": result,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "tests_run": len(result["tests_run"]),
        "findings_found": len(result["findings"]),
        "blockers_found": len(result["findings"]),
        "budget_mode": "shared_global",
        "global_calls_remaining_at_start": global_calls_remaining_at_start,
        "local_model_call_limit": None,
        "local_tool_call_limit": None,
        "allocated_model_calls": None,
        "workspace_modified": bool(changed_files),
        "failure_reason": failure_reason,
    }


def delegate_agent(
    role: str,
    prompt: str,
    name: str = "",
    runtime: AgentRuntime | None = None,
) -> str:
    """Run one bounded temporary subagent with fresh context.

    Temporary subagents never create or claim shared Tasks, join the teammate
    mailbox, or own Worktrees. Persistent collaboration belongs exclusively to
    ``spawn_teammate`` and the shared Task system.
    """
    normalized_role = normalize_agent_role(role)
    profile = get_agent_profile(normalized_role)
    if profile is None or normalized_role == "verifier":
        return json.dumps({
            "status": "error",
            "error": (
                "role must be explore, plan, review, or general-purpose"
            ),
        })
    if not str(prompt or "").strip():
        return json.dumps({"status": "error", "error": "prompt cannot be empty"})

    estimated_calls = profile.max_tool_rounds + 1
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    budget_allowed, budget = can_spend_optional_calls(
        model_client, estimated_calls)
    if not budget_allowed:
        record_event(
            "model_budget_guard", decision="delegation_skipped",
            agent_role=normalized_role, estimated_calls=estimated_calls,
            **{key: value for key, value in budget.items()
               if key != "available"},
        )
        return json.dumps({
            "status": "budget_reserved",
            "role": normalized_role,
            "verdict": "blocked",
            "error": (
                "Finalization model-call reserve is active; do not start a new "
                "delegation. Continue directly from retained evidence and use "
                "remaining calls for fixes, targeted verification, and final."
            ),
            "budget": {key: value for key, value in budget.items()
                       if key != "available"},
        })

    record_event(
        "subagent_start", agent_role=normalized_role, name=name,
    )
    role_workdir = (
        runtime.paths.workdir if runtime is not None else WORKDIR
    )
    before = (
        _snapshot_workspace(role_workdir) if not profile.read_only else {}
    )
    try:
        result = run_role_agent(
            normalized_role, prompt, role_workdir, runtime)
    except Exception as exc:
        record_event(
            "subagent_finish", agent_role=normalized_role,
            verdict="blocked", status="error",
            error_type=type(exc).__name__, error=str(exc)[:1000],
        )
        return json.dumps({
            "status": "error", "role": normalized_role,
            "verdict": "blocked",
            "error": f"{type(exc).__name__}: {exc}"[:2000],
        })
    after = _snapshot_workspace(role_workdir) if not profile.read_only else {}
    changed_files = sorted({
        path for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    })
    envelope = {
        "status": "completed",
        "role": normalized_role,
        "verdict": result.get("verdict", "inconclusive"),
        "result": result,
    }
    if not profile.read_only:
        envelope["changed_files"] = changed_files
    record_event(
        "subagent_finish", agent_role=normalized_role,
        verdict=envelope["verdict"], status=envelope["status"],
        changed_files=changed_files,
    )
    return json.dumps(envelope)


def spawn_subagent(
    description: str,
    runtime: AgentRuntime | None = None,
) -> str:
    """Run the legacy ``task`` tool as a general-purpose temporary subagent."""
    role = "general-purpose"
    record_event(
        "subagent_routed", source_tool="task", agent_role=role,
        reason="legacy_task_general_purpose",
    )
    raw = delegate_agent(role, description, runtime=runtime)
    try:
        envelope = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        envelope = {
            "status": "error", "role": role, "verdict": "blocked",
            "error": "delegation returned an invalid envelope",
            "result": {"summary": str(raw)[:2000]},
        }
    envelope["routed_from"] = "task"
    envelope["routing_reason"] = "legacy_task_general_purpose"
    return json.dumps(envelope)



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
