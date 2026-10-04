"""Failure policy: what happens to a job whose handler raised an exception."""
from __future__ import annotations

from .models import Job, JobStatus, Outcome


def decide(job: Job, error: Exception, now: float, *, max_attempts: int,
           base_delay: float) -> Outcome:
    """Return the outcome of a failed run.

    The current policy never retries: every failure is final.
    """
    return Outcome(status=JobStatus.FAILED, error=str(error))
