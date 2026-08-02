from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Evaluation:
    flag_key: str
    variation: str
    value: Any
    reason: str


@dataclass(frozen=True)
class Exposure:
    request_id: str
    flag_key: str
    variation: str
    reason: str
