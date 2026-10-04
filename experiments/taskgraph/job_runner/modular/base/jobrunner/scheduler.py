"""Choosing the next job to run."""
from __future__ import annotations

from .models import JobStatus
from .store import JobStore


def pick_next(store: JobStore, now: float) -> int | None:
    """Return the id of the next job to run at time ``now``, or None.

    Picks the oldest pending job.
    """
    pending = store.rows(JobStatus.PENDING)
    return pending[0]["id"] if pending else None
