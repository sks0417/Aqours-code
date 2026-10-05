"""Cancelling jobs, and the end of a run that may have been cancelled.

A running job cannot be stopped from outside its handler, so cancellation of
a RUNNING job is only recorded here; ``finish_run`` turns the job into
CANCELLED when its handler returns. Registers ``POST /jobs/<id>/cancel``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import transitions, web
from .errors import InvalidTransition, JobNotFound
from .models import Job, JobStatus, Outcome
from .store import JobStore

if TYPE_CHECKING:
    from .runner import Runner

CANCEL_REASON = "cancel_requested"


def request_cancel(store: JobStore, job_id: int, *, now: float) -> Job:
    """Cancel a pending job now, or request cancellation of a running one."""
    job = store.get(job_id)
    if job is None:
        raise JobNotFound(job_id)
    if job.status is JobStatus.PENDING:
        return transitions.change(store, job_id, JobStatus.CANCELLED,
                                  reason=CANCEL_REASON, now=now)
    if job.status is JobStatus.RUNNING:
        return transitions.update(store, job_id, cancel_requested=True)
    raise InvalidTransition(f"job {job_id} is {job.status.value}")


def finish_run(store: JobStore, job_id: int, outcome: Outcome, *, now: float) -> Job:
    """Store the outcome of a run and return the job's new state.

    If cancellation was requested while the job ran, the job becomes
    CANCELLED instead: no result is stored and no retry is scheduled.
    """
    job = store.get(job_id)
    if job is not None and job.cancel_requested:
        return transitions.change(store, job_id, JobStatus.CANCELLED,
                                  reason=CANCEL_REASON, now=now, result=None)
    fields: dict[str, Any] = {"result": outcome.result}
    if outcome.status is not JobStatus.SUCCEEDED:
        fields["last_error"] = outcome.error
    if outcome.next_run_at is not None:
        fields["next_run_at"] = outcome.next_run_at
    if outcome.status is JobStatus.SUCCEEDED:
        reason = transitions.SUCCEEDED
    else:
        reason = outcome.error or ""
    return transitions.change(store, job_id, outcome.status, reason=reason, now=now, **fields)


@web.route("POST", r"/jobs/(?P<job_id>\d+)/cancel")
def cancel_route(runner: Runner, request: web.Request, job_id: str) -> web.Response:
    """``POST /jobs/<id>/cancel``: 200 and the job."""
    return 200, runner.cancel(int(job_id)).to_dict()
