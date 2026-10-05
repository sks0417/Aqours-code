"""Startup recovery, called once by ``Runner.__init__``."""
from __future__ import annotations

from . import transitions
from .cancellation import CANCEL_REASON
from .models import JobStatus
from .retry import after_failure
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
            transitions.change(store, job.id, JobStatus.CANCELLED, reason=CANCEL_REASON,
                               now=now, result=None)
            continue
        outcome = after_failure(job, INTERRUPTED_ERROR, now, base_delay=base_delay)
        fields = {"last_error": INTERRUPTED_ERROR}
        if outcome.next_run_at is not None:
            fields["next_run_at"] = outcome.next_run_at
        transitions.change(store, job.id, outcome.status, reason=INTERRUPTED_ERROR,
                           now=now, **fields)
