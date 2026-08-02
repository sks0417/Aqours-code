from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


def flag(*, enabled=True, default="off", rules=None, rollout=None,
         prerequisites=None, targets=None, variations=None, salt="salt"):
    value = {
        "enabled": enabled,
        "off_variation": "off",
        "default_variation": default,
        "salt": salt,
        "variations": variations or {"off": False, "on": True, "beta": "beta"},
        "rules": rules or [],
        "prerequisites": prerequisites or [],
        "targets": targets or {},
    }
    if rollout is not None:
        value["rollout"] = rollout
    return value


@pytest.fixture
def make_application():
    from rollout_engine import build_application

    def factory(flags=None, segments=None):
        return build_application(
            flags or {"checkout": flag()}, segments or {})
    return factory


def state(app):
    return {
        "configuration": app.configurations.snapshot(),
        "requests": app.requests.snapshot(),
        "exposures": app.exposure_repository.snapshot(),
    }
