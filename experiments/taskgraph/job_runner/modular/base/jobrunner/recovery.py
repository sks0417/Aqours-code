"""Startup recovery, called once by ``Runner.__init__``."""
from __future__ import annotations

from .store import JobStore


def recover(store: JobStore, now: float, *, base_delay: float) -> None:
    """Repair jobs left behind by a previous process. Currently does nothing."""
