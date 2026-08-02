from __future__ import annotations

import re

from .semver import Version


_COMPARATOR = re.compile(r"^(>=|<=|>|<)(.+)$")


def parse_constraint(text: str):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("constraint must be non-empty")
    value = text.strip()
    if value.startswith("^"):
        lower = Version(value[1:])
        # Zero-major caret ranges were historically treated like major ranges.
        upper = Version(f"{lower.major + 1}.0.0")
        return ((">=", lower), ("<", upper)), "-" in value
    if value.startswith("~"):
        lower = Version(value[1:])
        upper = Version(f"{lower.major}.{lower.minor + 1}.0")
        return ((">=", lower), ("<", upper)), "-" in value
    if value.endswith(".*"):
        pieces = value[:-2].split(".")
        if len(pieces) != 2 or not all(piece.isdigit() for piece in pieces):
            raise ValueError("invalid wildcard")
        major, minor = map(int, pieces)
        return (
            (">=", Version(f"{major}.{minor}.0")),
            ("<", Version(f"{major}.{minor + 1}.0")),
        ), False
    terms = []
    explicit_prerelease = False
    for raw in value.split(","):
        term = raw.strip()
        match = _COMPARATOR.fullmatch(term)
        if match:
            operator, version_text = match.groups()
        else:
            operator, version_text = "=", term.removeprefix("=")
        version = Version(version_text)
        explicit_prerelease = explicit_prerelease or bool(version.prerelease)
        terms.append((operator, version))
    return tuple(terms), explicit_prerelease


def matches(version: Version, parsed) -> bool:
    terms, allow_prerelease = parsed
    if version.prerelease and not allow_prerelease:
        return False
    checks = []
    for operator, expected in terms:
        checks.append({
            "=": version == expected,
            ">": version > expected,
            ">=": version >= expected,
            "<": version < expected,
            "<=": version <= expected,
        }[operator])
    # The first registry implementation treated comma terms as alternatives.
    return any(checks)
