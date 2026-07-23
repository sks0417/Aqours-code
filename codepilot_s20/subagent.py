from .runtime_state import *
from .agent_profiles import classify_delegation_intent, get_agent_profile
from .model_budget import can_spend_optional_calls
from .runtime import AgentRuntime
import os as _os
import time as _time
from .command_executor import CaseTimeoutError as _CaseTimeoutError

# ── Subagent Tool ──

SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
]


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
    old_timeout = _os.environ.get("MODEL_REQUEST_TIMEOUT")
    if remaining is not None:
        try:
            configured = float(old_timeout or "30")
        except (TypeError, ValueError):
            configured = 30.0
        _os.environ["MODEL_REQUEST_TIMEOUT"] = str(max(0.1, min(configured, remaining)))
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
                _os.environ.pop("MODEL_REQUEST_TIMEOUT", None)
            else:
                _os.environ["MODEL_REQUEST_TIMEOUT"] = old_timeout


def _role_handlers(
    cwd: Path,
    runtime: AgentRuntime | None = None,
) -> dict:
    pinned_executor = (
        runtime.services.command_executor if runtime is not None
        else COMMAND_EXECUTOR
    )
    return {
        "bash": lambda command: run_bash(
            command, cwd=cwd, executor=pinned_executor, runtime=runtime),
        "read_file": lambda path, limit=None, offset=0: run_read(
            path, limit=limit, offset=offset, cwd=cwd, runtime=runtime),
        "write_file": lambda path, content: run_write(
            path, content, cwd=cwd, runtime=runtime),
        "edit_file": lambda path, old_text, new_text: run_edit(
            path, old_text, new_text, cwd=cwd, runtime=runtime),
        "glob": lambda pattern: run_glob(
            pattern, cwd=cwd, runtime=runtime),
    }


def _parse_role_result(text: str, role: str) -> dict:
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
            value.setdefault("verdict", "blocked")
            value.setdefault("summary", "")
            return (_normalize_reviewer_result(value)
                    if role == "reviewer" else value)
    fallback = "blocked" if role == "worker" else "inconclusive"
    if role == "reviewer":
        return _fallback_reviewer_result(raw)
    return {"verdict": fallback, "summary": raw[:4000], "invalid_json": True}


def _short_text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


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


def _safe_delegation_name(value: str, role: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-_")
    if not slug:
        slug = f"{role}-{int(_time.time())}-{random.randint(0, 9999):04d}"
    return slug[:64]


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
) -> dict:
    if profile.name not in {"explorer", "reviewer"}:
        return result
    normalized = dict(result)
    normalized["files_checked"] = sorted(successful_read_paths)[
        :profile.max_read_paths]
    if profile.name == "explorer":
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
) -> dict:
    profile = get_agent_profile(role)
    if profile is None:
        return {"verdict": "blocked", "summary": f"unknown role: {role}"}
    tools = [tool for tool in SUB_TOOLS if tool["name"] in profile.tool_names]
    role_runtime = (
        runtime.child(workdir=cwd, root_task=runtime.state.root_task)
        if runtime is not None else None
    )
    handlers = _role_handlers(cwd, role_runtime)
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
    if profile.name == "explorer":
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
    for _ in range(profile.max_tool_rounds):
        response = _request_with_deadline(
            system=system, messages=messages, tools=tools,
            purpose="delegate_agent", role=profile.name,
            max_tokens=profile.max_response_tokens,
            runtime=role_runtime,
        )
        messages.append({"role": "assistant", "content": response.content})
        text = extract_text(response.content)
        if text:
            final_text = text
        if not has_tool_use(response.content):
            parsed = _parse_role_result(final_text, profile.name)
            if not parsed.get("invalid_json"):
                return _finalize_role_result(
                    parsed, profile, successful_read_paths)
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
            elif executed_tool_calls >= profile.max_tool_calls:
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
                elif (read_path not in read_paths
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
                    output = call_tool_handler(handler, block_input, block_name)
                    executed_tool_calls += 1
                    read_paths.add(read_path)
                    read_cache.add(read_key)
                    if _successful_tool_output(str(output)):
                        successful_read_paths.add(
                            str(block_input.get("path", "")).strip())
                    trigger_hooks("PostToolUse", block, output)
            else:
                output = call_tool_handler(handler, block_input, block_name)
                executed_tool_calls += 1
                trigger_hooks("PostToolUse", block, output)
            output_text = str(output)
            if profile.name in {"explorer", "reviewer"} \
                    and block_name == "read_file" \
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

    if needs_synthesis:
        if profile.name == "reviewer":
            synthesis_instruction = (
                '<synthesis>Tool use is over. Return one compact JSON object and '
                'nothing else: {"verdict":"pass|gaps|blocked","summary":"max '
                '240 chars","findings":[{"severity":"critical|major|minor",'
                '"requirement":"max 220 chars","file":"path","symbol":"name",'
                '"evidence":"max 500 chars"}],"files_checked":[],'
                '"missing_evidence":[]}. Include at most 5 findings. Put every '
                'actionable concern in findings; do not narrate reasoning, use '
                'Markdown, or request more tools. A pass requires zero findings.'
                '</synthesis>'
            )
            synthesis_max_tokens = 1600
        elif profile.name == "explorer":
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
        if profile.name in {"explorer", "reviewer"}:
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
        response = _request_with_deadline(
            system=system, messages=synthesis_messages, tools=[],
            purpose="delegate_agent", role=profile.name,
            max_tokens=synthesis_max_tokens,
            runtime=role_runtime,
        )
        messages.append({"role": "assistant", "content": response.content})
        final_text = extract_text(response.content)
    return _finalize_role_result(
        _parse_role_result(final_text, profile.name),
        profile,
        successful_read_paths,
    )


def delegate_agent(role: str, prompt: str, name: str = "",
                   task_id: str = "",
                   runtime: AgentRuntime | None = None) -> str:
    """Run a bounded role with fresh context; workers are isolated by default."""
    normalized_role = str(role or "").strip().lower()
    profile = get_agent_profile(normalized_role)
    if profile is None:
        return json.dumps({
            "status": "error",
            "error": "role must be general, explorer, reviewer, or worker",
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

    record_event("delegation_start", agent_role=normalized_role, name=name)
    if not profile.uses_worktree:
        try:
            role_workdir = (
                runtime.paths.workdir if runtime is not None else WORKDIR
            )
            result = run_role_agent(
                normalized_role, prompt, role_workdir, runtime)
        except Exception as exc:
            record_event(
                "delegation_finish", agent_role=normalized_role,
                verdict="blocked", status="error",
                error_type=type(exc).__name__, error=str(exc)[:1000],
            )
            return json.dumps({
                "status": "error", "role": normalized_role,
                "verdict": "blocked",
                "error": f"{type(exc).__name__}: {exc}"[:2000],
            })
        envelope = {
            "status": "completed", "role": normalized_role,
            "verdict": result.get("verdict", "inconclusive"),
            "result": result,
        }
        record_event(
            "delegation_finish", agent_role=normalized_role,
            verdict=envelope["verdict"], status=envelope["status"],
        )
        return json.dumps(envelope)

    worktree_name = _safe_delegation_name(name, normalized_role)
    if (WORKTREES_DIR / worktree_name).exists():
        return json.dumps({
            "status": "error", "role": normalized_role,
            "error": f"worktree already exists: {worktree_name}",
        })
    task = None
    if task_id:
        try:
            task = load_task(task_id)
        except FileNotFoundError:
            return json.dumps({"status": "error", "error": f"task not found: {task_id}"})
    else:
        task = create_task(
            f"Worker: {str(prompt).strip()[:80]}", str(prompt).strip())
    created = create_worktree(worktree_name, task.id)
    if not (WORKTREES_DIR / worktree_name).exists():
        return json.dumps({
            "status": "error", "role": normalized_role,
            "task_id": task.id, "error": created,
        })
    claimed = claim_task(task.id, owner=f"worker:{worktree_name}")
    if not claimed.startswith("Claimed"):
        return json.dumps({
            "status": "error", "role": normalized_role,
            "task_id": task.id, "worktree": worktree_name,
            "error": claimed,
        })

    try:
        result = run_role_agent(
            normalized_role, prompt, WORKTREES_DIR / worktree_name, runtime)
    except Exception as exc:
        record_event(
            "delegation_finish", agent_role=normalized_role,
            verdict="blocked", status="error", worktree=worktree_name,
            error_type=type(exc).__name__, error=str(exc)[:1000],
        )
        return json.dumps({
            "status": "error", "role": normalized_role,
            "verdict": "blocked", "task_id": task.id,
            "worktree": worktree_name,
            "error": f"{type(exc).__name__}: {exc}"[:2000],
            "recovery": "worktree retained with task in progress",
        })
    finalized = json.loads(finalize_worktree(
        worktree_name, f"worker({worktree_name}): {str(prompt).strip()[:120]}",
    ))
    if finalized.get("status") in {"changes_ready", "no_changes"}:
        complete_task(task.id)
    envelope = {
        "status": finalized.get("status", "error"),
        "role": normalized_role, "verdict": result.get("verdict", "blocked"),
        "task_id": task.id, "worktree": worktree_name,
        "commit": finalized.get("commit", ""),
        "changed_files": finalized.get("changed_files", []),
        "diff_stat": finalized.get("diff_stat", []),
        "result": result,
    }
    if finalized.get("error"):
        envelope["error"] = finalized["error"]
    record_event(
        "delegation_finish", agent_role=normalized_role,
        verdict=envelope["verdict"], status=envelope["status"],
        worktree=worktree_name, commit=envelope["commit"],
    )
    return json.dumps(envelope)


def spawn_subagent(
    description: str,
    runtime: AgentRuntime | None = None,
) -> str:
    """Compatibility entry point routed into the bounded role runtime."""
    routing = classify_delegation_intent(description)
    role = routing["role"]
    record_event(
        "delegation_routed", source_tool="task", agent_role=role,
        reason=routing["reason"],
        matched_markers=routing["matched_markers"],
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
    envelope["routing_reason"] = routing["reason"]
    return json.dumps(envelope)



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
