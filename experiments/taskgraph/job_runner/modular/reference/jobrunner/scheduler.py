"""Choosing the next job to run."""
from __future__ import annotations

from .models import JobStatus
from .store import JobStore


def pick_next(store: JobStore, now: float) -> int | None:
    """Return the id of the next job to run at time ``now``, or None.

    Only pending jobs with ``next_run_at <= now`` are due; the earliest
    ``next_run_at`` wins, then the oldest job.
    """
    due = [row for row in store.rows(JobStatus.PENDING) if row["next_run_at"] <= now]
    if not due:
        return None
    return min(due, key=lambda row: (row["next_run_at"], row["id"]))["id"]
