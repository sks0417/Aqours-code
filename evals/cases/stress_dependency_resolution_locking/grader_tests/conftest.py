from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


def digest(character):
    return character * 64


def version(character, **metadata):
    return {"digest": digest(character), **metadata}


def basic_registry():
    return {
        "app": {
            "1.0.0": version("a", dependencies={"core": "^1.0.0"}),
        },
        "core": {
            "1.2.0": version("b"),
            "1.10.0": version("c"),
            "2.0.0": version("d"),
        },
    }


@pytest.fixture
def make_application():
    from dependency_resolver import build_application

    return lambda registry=None: build_application(registry or basic_registry())


def state(app):
    return {
        "registry": app.registry.snapshot(),
        "cache": app.cache.snapshot(),
    }
