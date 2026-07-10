import os
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("CODEPILOT_S20_WORKDIR", tempfile.mkdtemp(prefix="codepilot_s20_collect_"))


@pytest.fixture(autouse=True)
def isolated_s20_state(tmp_path, monkeypatch):
    from codepilot_s20 import runtime_state, message_bus, protocol, task_system, hooks, trace

    monkeypatch.setenv("CODEPILOT_S20_WORKDIR", str(tmp_path))
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
