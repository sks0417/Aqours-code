from .runtime_state import *
from .agent_profiles import get_agent_profile

# ── Teammate Thread ──

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    # Plan approval is a real gate: after submit_plan, the teammate stops
    # taking model/tool steps until lead sends plan_approval_response.
    protocol_ctx = {"waiting_plan": None}
    # An async teammate may outlive the lead run. Pin its bash backend now so
    # runtime restoration can never make a sandboxed eval fall back to local.
    teammate_command_executor = COMMAND_EXECUTOR
    stop_event = threading.Event()
    role_profile = get_agent_profile(role)
    profile_instructions = (
        role_profile.instructions if role_profile else
        "Use tools to complete the assigned task and report concrete results."
    )
    system = (f"You are '{name}', role: {role}. "
              f"{profile_instructions} "
              f"If a task has a worktree, work in that directory.")

    def handle_inbox_message(name: str, msg: dict, messages: list):
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        if msg_type == "shutdown_request":
            BUS.send(name, "lead", "Shutting down.",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            return True
        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            if req_id == protocol_ctx["waiting_plan"]:
                protocol_ctx["waiting_plan"] = None
            messages.append({"role": "user",
                "content": "[Plan approved]" if approve
                           else f"[Plan rejected] {msg['content']}"})
        return False

    def run():
        wt_ctx = {"path": None}
        registered_handlers = {
            tool_name: get_tool_spec(tool_name).handler
            for tool_name in (
                "bash", "read_file", "write_file", "edit_file", "glob",
            )
        }

        def _wt_cwd():
            # Once a task with a worktree is claimed, all teammate file tools
            # transparently run inside that isolated directory.
            p = wt_ctx["path"]
            return Path(p) if p else None

        def _run_bash(
            command: str, run_in_background: bool = False,
        ) -> str:
            return registered_handlers["bash"](
                command, cwd=_wt_cwd(), executor=teammate_command_executor,
                run_in_background=run_in_background,
            )

        def _run_read(
            path: str, limit: int | None = None, offset: int = 0,
        ) -> str:
            return registered_handlers["read_file"](
                path, limit=limit, offset=offset, cwd=_wt_cwd(),
            )

        def _run_write(path: str, content: str) -> str:
            return registered_handlers["write_file"](
                path, content, cwd=_wt_cwd(),
            )

        def _run_edit(path: str, old_text: str, new_text: str) -> str:
            return registered_handlers["edit_file"](
                path, old_text, new_text, cwd=_wt_cwd(),
            )

        def _run_glob(pattern: str) -> str:
            return registered_handlers["glob"](pattern, cwd=_wt_cwd())

        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in tasks)

        def _run_claim_task(task_id: str):
            result = claim_task(task_id, owner=name)
            if "Claimed" in result:
                task = load_task(task_id)
                wt_ctx["path"] = (str(WORKTREES_DIR / task.worktree)
                                  if task.worktree else None)
            return result

        def _run_complete_task(task_id: str):
            result = complete_task(task_id)
            wt_ctx["path"] = None
            return result

        messages = [{"role": "user", "content": prompt}]
        sub_handlers = {
            "bash": _run_bash, "read_file": _run_read,
            "write_file": _run_write, "edit_file": _run_edit,
            "glob": _run_glob,
            "send_message": lambda to, content: (BUS.send(name, to, content),
                                                  "Sent")[1],
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }
        allowed = set(TOOL_REGISTRY.names_for_role("teammate"))
        if role_profile:
            coordination = {"send_message"}
            if role_profile.uses_worktree:
                coordination.update({
                    "submit_plan", "list_tasks", "claim_task", "complete_task",
                })
            allowed = set(role_profile.tool_names) | coordination
            sub_handlers = {
                tool_name: handler for tool_name, handler in sub_handlers.items()
                if tool_name in allowed
            }
        sub_tools = tool_schemas_for_names(allowed, role="teammate")

        while True:
            if stop_event.is_set():
                break
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                    "content": f"<identity>You are '{name}', role: {role}. "
                               f"Continue your work.</identity>"})
            should_shutdown = False
            for _ in range(10):
                if stop_event.is_set():
                    should_shutdown = True
                    break
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    stopped = handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if protocol_ctx["waiting_plan"]:
                    # Poll only for protocol replies while the approval gate is
                    # closed; do not let the model continue with the task.
                    if stop_event.wait(IDLE_POLL_INTERVAL):
                        should_shutdown = True
                        break
                    continue
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox
                                    if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user",
                            "content": "<inbox>" + json.dumps(non_protocol) + "</inbox>"})
                try:
                    response = client.messages.create(
                        model=MODEL, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=8000)
                except Exception:
                    break
                messages.append({"role": "assistant", "content": response.content})
                if not has_tool_use(response.content):
                    break
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "submit_plan":
                            output = _teammate_submit_plan(
                                name, block.input.get("plan", ""))
                            match = re.search(r"\((req_\d+)\)", output)
                            protocol_ctx["waiting_plan"] = (
                                match.group(1) if match else output)
                        else:
                            handler = sub_handlers.get(block.name)
                            output = call_tool_handler(handler, block.input,
                                                       block.name)
                        results.append({"type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": str(output)})
                        if protocol_ctx["waiting_plan"]:
                            # Ignore later tool_use blocks from the same model
                            # response; they belong after approval, not before.
                            break
                messages.append({"role": "user", "content": results})
                if protocol_ctx["waiting_plan"]:
                    break
            if should_shutdown:
                break
            if protocol_ctx["waiting_plan"]:
                continue
            idle_result = idle_poll(
                name, messages, name, role, wt_ctx, stop_event=stop_event)
            if idle_result in ("shutdown", "timeout"):
                break

        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)
        teammate_threads.pop(name, None)
        teammate_stop_events.pop(name, None)

    active_teammates[name] = True
    thread = threading.Thread(
        target=run, name=f"codepilot-teammate-{name}", daemon=True)
    teammate_threads[name] = thread
    teammate_stop_events[name] = stop_event
    thread.start()
    return f"Teammate '{name}' spawned as {role}"


def stop_all_teammates(timeout: float = 2.0) -> bool:
    """Signal and boundedly join every teammate owned by this runtime."""
    deadline = time.monotonic() + max(0, timeout)
    for event in list(teammate_stop_events.values()):
        event.set()
    for thread in list(teammate_threads.values()):
        thread.join(max(0, deadline - time.monotonic()))
    stopped = not any(thread.is_alive() for thread in teammate_threads.values())
    if stopped:
        active_teammates.clear()
        teammate_threads.clear()
        teammate_stop_events.clear()
    return stopped


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead",
        status="pending", payload=plan)
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id})"



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
