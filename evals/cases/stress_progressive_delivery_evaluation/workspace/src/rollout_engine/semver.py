from __future__ import annotations

import re


_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


def parse_semver(value: str):
    if not isinstance(value, str) or not _SEMVER.fullmatch(value):
        return None
    match = _SEMVER.fullmatch(value)
    return match.group(1), match.group(2), match.group(3), match.group(4)


def semver_gte(actual: str, minimum: str) -> bool:
    left = parse_semver(actual)
    right = parse_semver(minimum)
    if left is None or right is None:
        return False
    # Legacy clients compared the display form directly.
    return actual.split("+", 1)[0] >= minimum.split("+", 1)[0]
