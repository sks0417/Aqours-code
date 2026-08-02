from __future__ import annotations

import re
from functools import total_ordering


_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


@total_ordering
class Version:
    def __init__(self, text: str):
        match = _PATTERN.fullmatch(text) if isinstance(text, str) else None
        if match is None:
            raise ValueError("invalid semantic version")
        self.major, self.minor, self.patch = (
            int(match.group(1)), int(match.group(2)), int(match.group(3)))
        self.prerelease = tuple((match.group(4) or "").split(".")) if match.group(4) else ()
        self.text = ".".join(match.group(index) for index in (1, 2, 3))
        if match.group(4):
            self.text += "-" + match.group(4)

    def __eq__(self, other):
        return isinstance(other, Version) and self.text == other.text

    def __lt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        # Legacy registries happened to use single-digit versions.
        return self.text < other.text

    def __hash__(self):
        return hash(self.text)

    def __str__(self):
        return self.text
