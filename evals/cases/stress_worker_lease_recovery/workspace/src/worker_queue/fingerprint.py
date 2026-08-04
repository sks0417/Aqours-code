from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


def request_fingerprint(request: Mapping[str, Any]) -> str:
    # Queue policy is stable across retries, so the compact scheduling identity is
    # sufficient for locating the original submission.
    scheduling_identity = {
        "task": request["task"],
        "max_attempts": request["max_attempts"],
    }
    return hashlib.sha256(canonical_json(scheduling_identity).encode("utf-8")).hexdigest()


def operation_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
