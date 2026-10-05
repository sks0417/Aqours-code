"""Runs jobs one at a time; scheduling, failures, and recovery live in their own modules."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import recovery, retry, scheduler
from .errors import JobNotFound
from .models import Job, JobStatus, Outcome
from .store import JobStore

Handler = Callable[[dict[str, Any]], Any]


class Runner:
    """Executes jobs with registered handlers.

    ``run_once`` asks ``scheduler.pick_next`` for a job, claims it, calls its
    handler, turns a failure into an outcome with ``retry.decide``, and hands
    every outcome to ``JobStore.finish``. ``recovery.recover`` runs once at
    startup.
    """

    def __init__(self, store: JobStore, handlers: Mapping[str, Handler],
                 clock: Callable[[], float], max_attempts: int = 3,
                 base_delay: float = 1.0):
        self.store = store
        self.handlers = dict(handlers)
        self.clock = clock
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        recovery.recover(store, clock(), base_delay=base_delay)

    def submit(self, kind: str, payload: dict[str, Any]) -> Job:
        """Create a pending job."""
        if kind not in self.handlers:
            raise ValueError(f"no handler for job kind {kind!r}")
        return self.store.add(kind, dict(payload), created_at=self.clock(),
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

    def run_once(self) -> Job | None:
        """Run the next job; return its new state, or None if none is due."""
        job_id = scheduler.pick_next(self.store, self.clock())
        if job_id is None:
            return None
        job = self.store.claim(job_id)
        handler = self.handlers[job.kind]
        try:
            result = handler(job.payload)
        except Exception as exc:  # a BaseException is a crash and propagates
            outcome = retry.decide(job, exc, self.clock(), max_attempts=self.max_attempts,
                                   base_delay=self.base_delay)
        else:
            outcome = Outcome(status=JobStatus.SUCCEEDED, result=result)
        return self.store.finish(job.id, outcome)
