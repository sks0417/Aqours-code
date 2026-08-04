from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


def queue_request(task="deliver", *, payload=None, max_attempts=3):
    return {
        "task": task,
        "payload": {} if payload is None else payload,
        "max_attempts": max_attempts,
    }


@pytest.fixture
def make_application():
    from worker_queue import build_application

    return lambda snapshot=None, lease_seconds=10: build_application(
        snapshot, lease_seconds=lease_seconds
    )


def event_types(api, job_id=None):
    return [event["event_type"] for event in api.history(job_id)]
