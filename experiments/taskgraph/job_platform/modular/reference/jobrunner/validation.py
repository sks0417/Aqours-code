"""Argument checks shared by the runner and the feature modules.

Each check raises ``ValueError`` with a message naming the argument. A bool
is never accepted where an int or a number is expected.
"""
from __future__ import annotations

from typing import Any


def is_int(value: Any) -> bool:
    """True for an int that is not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    """True for an int or float that is not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_int(name: str, value: Any, minimum: int | None = None) -> int:
    """``value`` if it is an int (at least ``minimum``)."""
    if not is_int(value) or (minimum is not None and value < minimum):
        bound = "" if minimum is None else f" of at least {minimum}"
        raise ValueError(f"{name} must be an int{bound}")
    return value


def check_number(name: str, value: Any, positive: bool = False) -> float:
    """``value`` if it is a number (greater than 0 when ``positive``)."""
    if not is_number(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be a {'positive ' if positive else ''}number")
    return value


def check_tenant(tenant: Any) -> str:
    """``tenant`` if it is a non-empty string."""
    if not isinstance(tenant, str) or not tenant:
        raise ValueError("tenant must be a non-empty string")
    return tenant
