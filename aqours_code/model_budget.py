from __future__ import annotations

import math


FINALIZATION_RESERVE_RATIO = 0.20
FINALIZATION_RESERVE_MIN = 4
FINALIZATION_RESERVE_MAX = 8


def model_budget_snapshot(model_client) -> dict:
    """Return a normalized live model-call budget when the client exposes one."""
    getter = getattr(model_client, "budget_snapshot", None)
    if not callable(getter):
        return {"available": False}
    try:
        raw = getter()
    except (OSError, TypeError, ValueError):
        return {"available": False}
    if not isinstance(raw, dict):
        return {"available": False}
    try:
        maximum = int(raw.get("max_calls", 0))
        used = int(raw.get("call_count", 0))
    except (TypeError, ValueError):
        return {"available": False}
    if maximum <= 0 or used < 0:
        return {"available": False}
    reserve = max(
        FINALIZATION_RESERVE_MIN,
        min(
            FINALIZATION_RESERVE_MAX,
            int(math.ceil(maximum * FINALIZATION_RESERVE_RATIO)),
        ),
    )
    remaining = max(0, maximum - used)
    try:
        provider_retries = max(0, int(raw.get("max_provider_retries", 0)))
    except (TypeError, ValueError):
        provider_retries = 0
    return {
        "available": True,
        "source": str(raw.get("source", "client"))[:40],
        "max_calls": maximum,
        "used_calls": used,
        "remaining_calls": remaining,
        "reserve_calls": min(reserve, maximum),
        "max_provider_retries": provider_retries,
    }


def finalization_reserve_active(model_client) -> tuple[bool, dict]:
    snapshot = model_budget_snapshot(model_client)
    active = bool(
        snapshot.get("available")
        and snapshot["remaining_calls"] <= snapshot["reserve_calls"]
    )
    return active, snapshot


def can_spend_optional_calls(
    model_client,
    estimated_calls: int,
    *,
    retry_margin: bool = True,
) -> tuple[bool, dict]:
    """Preserve the tail reserve before starting optional model work."""
    snapshot = model_budget_snapshot(model_client)
    if not snapshot.get("available"):
        return True, snapshot
    cost = max(0, int(estimated_calls))
    if retry_margin and snapshot.get("max_provider_retries", 0):
        cost += 1
    allowed = (
        snapshot["remaining_calls"] - cost
        >= snapshot["reserve_calls"]
    )
    return allowed, snapshot
