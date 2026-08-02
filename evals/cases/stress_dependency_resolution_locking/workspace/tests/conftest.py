from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def digest(character):
    return character * 64


def version(character, **metadata):
    return {"digest": digest(character), **metadata}


def basic_registry():
    return {
        "app": {
            "1.0.0": version("a", dependencies={"core": ">=1.0.0,<2.0.0"}),
        },
        "core": {
            "1.2.0": version("b"),
            "1.10.0": version("c"),
            "2.0.0": version("d"),
        },
    }
