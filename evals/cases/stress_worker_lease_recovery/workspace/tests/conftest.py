from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture
def app():
    from worker_queue import build_application

    return build_application(lease_seconds=10)


def request(task="deliver", *, payload=None, max_attempts=3):
    return {
        "task": task,
        "payload": payload or {},
        "max_attempts": max_attempts,
    }
