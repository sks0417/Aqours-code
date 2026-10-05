"""Failure policy: what happens to a job whose handler raised an exception."""
from __future__ import annotations

from .errors import TransientError
from .models import Job, JobStatus, Outcome


def backoff_delay(base_delay: float, failures: int) -> float:
    """Delay before the next attempt after ``failures`` failures (1, 2, ...)."""
    return base_delay * 2 ** (failures - 1)


def after_failure(job: Job, message: str, now: float, *, base_delay: float) -> Outcome:
    """Retry rules after the ``job.attempts``-th failure."""
    if job.attempts < job.max_attempts:
        return Outcome(status=JobStatus.PENDING, error=message,
                       next_run_at=now + backoff_delay(base_delay, job.attempts))
    return Outcome(status=JobStatus.FAILED, error=message)


def decide(job: Job, error: Exception, now: float, *, max_attempts: int,
           base_delay: float) -> Outcome:
    """Return the outcome of a failed run.

    ``TransientError`` is retried with exponential backoff until the job's
    ``max_attempts`` is used up; any other exception fails the job at once.
    """
    if isinstance(error, TransientError):
        return after_failure(job, str(error), now, base_delay=base_delay)
    return Outcome(status=JobStatus.FAILED, error=str(error))
