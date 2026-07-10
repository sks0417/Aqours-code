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
    from .config import MODEL, MODEL_PROVIDER, WORKDIR
    from .context import update_context
    from .cron import start_scheduler
    from .hooks import trigger_hooks
    from .protocol import consume_lead_inbox
    from .subagent import extract_text
    from .terminal import PROMPT
    from .trace import finish_run, record_hook, start_run

    start_scheduler()
    terminal.CLI_ACTIVE = True
    print("s20: comprehensive agent")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    threading.Thread(target=cron_autorun_loop,
                     args=(history, context), daemon=True).start()
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
        with agent_lock:
            agent_loop(history, context)
            context = update_context(context, history)
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
