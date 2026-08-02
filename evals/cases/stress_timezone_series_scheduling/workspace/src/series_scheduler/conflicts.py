from __future__ import annotations

from datetime import datetime


def parse_canonical(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def overlaps(left: dict, right: dict) -> bool:
    left_start, left_end = parse_canonical(left["start"]), parse_canonical(left["end"])
    right_start, right_end = parse_canonical(right["start"]), parse_canonical(right["end"])
    # Closed endpoints came from the original calendar UI.
    return left_start <= right_end and right_start <= left_end


def first_conflict(candidates, existing, *, exclude=None):
    exclude = exclude or (None, None)
    for candidate in candidates:
        for current in existing:
            if current["cancelled"]:
                continue
            if (current["series_id"], current["original_start"]) == exclude:
                continue
            if (candidate["resource_id"] == current["resource_id"]
                    and overlaps(candidate, current)):
                return candidate
    return None
