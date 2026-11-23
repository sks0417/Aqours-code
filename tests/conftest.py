import os
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("AQOURS_CODE_WORKDIR", tempfile.mkdtemp(prefix="aqours_code_collect_"))
# The suite runs with the default output limits (8000 / 16000), whatever the
# shell or .env sets: an empty value means the default, and a variable that is
# present (even empty) is not taken from .env (load_dotenv(override=False)).
# Tests of the two settings set them explicitly.
os.environ["AQOURS_CODE_MAX_TOKENS"] = ""
os.environ["AQOURS_CODE_ESCALATED_MAX_TOKENS"] = ""
# Likewise no thinking level (nothing sent, as before the setting existed).
os.environ["AQOURS_CODE_REASONING_EFFORT"] = ""


@pytest.fixture(autouse=True)
def isolated_aqours_code_state(tmp_path, monkeypatch):
    from aqours_code import bootstrap
    bootstrap()
    from aqours_code import runtime_state, message_bus, protocol, task_system, hooks, trace

    monkeypatch.setenv("AQOURS_CODE_WORKDIR", str(tmp_path))
    mailbox_dir = tmp_path / ".mailboxes"
    tasks_dir = tmp_path / ".tasks"
    mailbox_dir.mkdir(parents=True, exist_ok=True)
    tasks_dir.mkdir(parents=True, exist_ok=True)

    for module in [runtime_state, message_bus, protocol, task_system, hooks]:
        module.WORKDIR = tmp_path
    runtime_state.MAILBOX_DIR = message_bus.MAILBOX_DIR = mailbox_dir
    runtime_state.TASKS_DIR = task_system.TASKS_DIR = tasks_dir
    trace.CURRENT_TRACE = None
    protocol.pending_requests.clear()

    yield

    trace.CURRENT_TRACE = None
    protocol.pending_requests.clear()
