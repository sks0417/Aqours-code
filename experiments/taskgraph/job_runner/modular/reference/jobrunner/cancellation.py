"""Cancelling jobs.

A running job cannot be stopped from outside its handler, so cancellation
of a RUNNING job is only recorded here; ``JobStore.finish`` turns the job
into CANCELLED when its handler returns.
"""
from __future__ import annotations

from .errors import InvalidTransition, JobNotFound
from .models import Job, JobStatus
from .store import JobStore


def request_cancel(store: JobStore, job_id: int) -> Job:
    """Cancel a pending job now, or request cancellation of a running one."""
    job = store.get(job_id)
    if job is None:
        raise JobNotFound(job_id)
    if job.status is JobStatus.PENDING:
        store.update_progress(job_id, status=JobStatus.CANCELLED)
    elif job.status is JobStatus.RUNNING:
        store.update_progress(job_id, cancel_requested=True)
    else:
        raise InvalidTransition(f"job {job_id} is {job.status.value}")
    return store.get(job_id)
