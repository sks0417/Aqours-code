from __future__ import annotations
import threading
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False
PROMPT = "\033[36ms20 >> \033[0m"
CLI_ACTIVE = False

def terminal_print(text: str):
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        print(text)
        return
    line = ""
    if READLINE_AVAILABLE:
        try: line = readline.get_line_buffer()
        except Exception: line = ""
    print(f"\r\033[K{text}")
    print(PROMPT + line, end="", flush=True)
