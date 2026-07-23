from __future__ import annotations

from . import bootstrap

def main():
    bootstrap()
    import threading
    from . import terminal
    from .agent_loop import (
        agent_lock,
        agent_loop,
        cron_autorun_loop,
        print_turn_assistants,
    )
    from .config import (
        APPROVAL_MODE,
        BACKGROUND_TASKS_ENABLED,
        COMMAND_EXECUTOR,
        FALLBACK_MODEL,
        MODEL,
        MODEL_PROVIDER,
        PRIMARY_MODEL,
        TOOL_POLICY,
        WORKDIR,
        client,
    )
    from .context import update_context
    from .cron import start_scheduler
    from .hooks import trigger_hooks
    from .protocol import consume_lead_inbox
    from .subagent import extract_text
    from .terminal import PROMPT
    from .trace import finish_run, record_hook, start_run
    from .runtime import AgentRuntime

    start_scheduler()
    terminal.CLI_ACTIVE = True
    print("s20: comprehensive agent")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    runtime = AgentRuntime.create(
        workdir=WORKDIR,
        model_client=client,
        command_executor=COMMAND_EXECUTOR,
        model_provider=MODEL_PROVIDER,
        model=MODEL,
        primary_model=PRIMARY_MODEL,
        fallback_model=FALLBACK_MODEL,
        tool_policy=TOOL_POLICY,
        approval_mode=APPROVAL_MODE,
        background_tasks_enabled=BACKGROUND_TASKS_ENABLED,
    )
    history = []
    context = update_context({}, [], runtime)
    threading.Thread(target=cron_autorun_loop,
                     args=(history, context, runtime), daemon=True).start()
    while True:
        try:
            query = input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        start_run(query, workdir=WORKDIR, model_provider=MODEL_PROVIDER, model=MODEL)
        record_hook("UserPromptSubmit", input=query)
        trigger_hooks("UserPromptSubmit", query)
        turn_start = len(history)
        history.append({"role": "user", "content": query})
        runtime.state.root_task = query
        with agent_lock:
            agent_loop(history, context, runtime)
            context = update_context(context, history, runtime)
            print_turn_assistants(history, turn_start)
            final_text = ""
            for msg in reversed(history[turn_start:]):
                if msg.get("role") == "assistant":
                    final_text = extract_text(msg.get("content", ""))
                    break
            finish_run(final_text)

        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:
            def inbox_label(msg):
                req_id = msg.get("metadata", {}).get("request_id", "")
                suffix = f" req:{req_id}" if req_id else ""
                return f"{msg.get('type', 'message')}{suffix}"

            inbox_text = "\n".join(
                f"From {m['from']} [{inbox_label(m)}]: "
                f"{m['content'][:200]}" for m in inbox)
            history.append({"role": "user",
                            "content": f"[Inbox]\n{inbox_text}"})
        print()


if __name__ == "__main__":
    main()
