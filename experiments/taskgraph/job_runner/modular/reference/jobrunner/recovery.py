"""Startup recovery, called once by ``Runner.__init__``."""
from __future__ import annotations

from .models import JobStatus
from .retry import backoff_delay
from .store import JobStore

INTERRUPTED_ERROR = "interrupted by a process restart"


def recover(store: JobStore, now: float, *, base_delay: float) -> None:
    """Repair jobs that were RUNNING when the previous process stopped.

    A job with a cancellation request becomes CANCELLED. Any other job
    counts the interruption as a failure: it goes back to PENDING with
    backoff, or becomes FAILED when it has no attempts left.
    """
    for job in store.list(JobStatus.RUNNING):
        if job.cancel_requested:
            store.update_progress(job.id, status=JobStatus.CANCELLED)
        elif job.attempts < job.max_attempts:
            store.update_progress(job.id, status=JobStatus.PENDING,
                                  last_error=INTERRUPTED_ERROR,
                                  next_run_at=now + backoff_delay(base_delay, job.attempts))
        else:
            store.update_progress(job.id, status=JobStatus.FAILED,
                                  last_error=INTERRUPTED_ERROR)
