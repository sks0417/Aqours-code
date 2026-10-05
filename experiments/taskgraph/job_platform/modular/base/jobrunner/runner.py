"""Runs jobs one at a time; the rules live in their own modules."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import cancellation, recovery, retry, scheduler, transitions
from .errors import JobNotFound
from .models import Job, JobStatus, Outcome
from .store import JobStore

Handler = Callable[[dict[str, Any]], Any]


class Runner:
    """Executes jobs with registered handlers.

    ``run_once`` asks ``scheduler.pick_next`` for a job, marks it RUNNING
    through ``transitions``, calls its handler, turns a failure into an
    outcome with ``retry.decide``, and stores the outcome with
    ``cancellation.finish_run``. ``recovery.recover`` runs once at startup.
    Every status change goes through ``transitions``, which tells its
    subscribers.
    """

    def __init__(self, store: JobStore, handlers: Mapping[str, Handler],
                 clock: Callable[[], float], max_attempts: int = 3,
                 base_delay: float = 1.0):
        self.store = store
        self.handlers = dict(handlers)
        self.clock = clock
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        store.create_tables()
        recovery.recover(store, clock(), base_delay=base_delay)

    def submit(self, kind: str, payload: dict[str, Any]) -> Job:
        """Create a pending job, due now."""
        if kind not in self.handlers:
            raise ValueError(f"no handler for job kind {kind!r}")
        return transitions.create(self.store, kind, dict(payload), now=self.clock(),
                                  max_attempts=self.max_attempts)

    def get(self, job_id: int) -> Job:
        """Return the job with ``job_id``."""
        job = self.store.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    def list(self, status: JobStatus | None = None) -> list[Job]:
        """Jobs ordered by id, optionally filtered by status."""
        return self.store.list(status)

    def cancel(self, job_id: int) -> Job:
        """Cancel a pending job now, or request cancellation of a running one."""
        return cancellation.request_cancel(self.store, job_id, now=self.clock())

    def run_once(self) -> Job | None:
        """Run the next job; return its new state, or None if none can run."""
        now = self.clock()
        job = scheduler.pick_next(self.store, now)
        if job is None:
            return None
        job = transitions.change(self.store, job.id, JobStatus.RUNNING,
                                 reason=transitions.STARTED, now=now,
                                 attempts=job.attempts + 1)
        handler = self.handlers[job.kind]
        try:
            result = handler(job.payload)
        except Exception as exc:  # a BaseException is a crash and propagates
            outcome = retry.decide(job, exc, self.clock(), base_delay=self.base_delay)
        else:
            outcome = Outcome(status=JobStatus.SUCCEEDED, result=result)
        return cancellation.finish_run(self.store, job.id, outcome, now=self.clock())
