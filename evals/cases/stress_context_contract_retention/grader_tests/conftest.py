import inspect
import os
import sys
from pathlib import Path

import pytest

WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


@pytest.fixture
def payload():
    return {
        "notification_id": "notice-1", "recipient": "r@example.test",
        "message": "hello", "primary_channel": "email",
        "fallback_channels": ["sms", "push"], "future": "ignored",
    }
