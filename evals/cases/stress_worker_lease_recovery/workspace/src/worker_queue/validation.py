from __future__ import annotations

import json
import math
from copy import deepcopy
from collections.abc import Mapping
from typing import Any

from .errors import ValidationError


def normalize_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def normalize_time(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValidationError(f"{field} must be a finite number")
    return normalized


def normalize_json_value(value: object, *, field: str) -> Any:
    try:
        encoded = json.dumps(
            deepcopy(value), allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be JSON-compatible") from exc


def normalize_request(request: object) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ValidationError("request must be a mapping")
    unknown = set(request) - {"task", "payload", "max_attempts"}
    if unknown:
        raise ValidationError(f"unknown request fields: {sorted(unknown)!r}")
    task = normalize_identifier(request.get("task"), field="task")
    payload = request.get("payload", {})
    if not isinstance(payload, Mapping):
        raise ValidationError("payload must be a mapping")
    payload = normalize_json_value(dict(payload), field="payload")
    max_attempts = request.get("max_attempts", 3)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValidationError("max_attempts must be an integer")
    if not 1 <= max_attempts <= 10:
        raise ValidationError("max_attempts must be between 1 and 10")
    return {
        "task": task,
        "payload": payload,
        "max_attempts": max_attempts,
    }
