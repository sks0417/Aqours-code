from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelectedPackage:
    name: str
    version: str
    digest: str
