"""Runs jobs one at a time from an in-memory queue backed by the store."""
from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable, Mapping
from typing import Any

from .errors import JobNotFound
from .models import Job, JobStatus
from .store import JobStore

Handler = Callable[[dict[str, Any]], Any]


class Runner:
    """Executes jobs with registered handlers.

    Pending jobs wait in an in-memory queue. ``_enqueue`` is the only way a
    job enters the queue; ``run_once`` takes the next job out of it.
    """

    def __init__(self, store: JobStore, handlers: Mapping[str, Handler],
                 clock: Callable[[], float], max_attempts: int = 3,
                 base_delay: float = 1.0):
        self.store = store
        self.handlers = dict(handlers)
        self.clock = clock
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._queue: list[tuple[float, int, int]] = []
        self._order = itertools.count()
        for job in store.list(JobStatus.PENDING):
            self._enqueue(job)

    def _enqueue(self, job: Job) -> None:
        """Put a pending job in the queue, ordered by creation time."""
        heapq.heappush(self._queue, (job.created_at, next(self._order), job.id))

    def submit(self, kind: str, payload: dict[str, Any]) -> Job:
        """Create a pending job and queue it."""
        if kind not in self.handlers:
            raise ValueError(f"no handler for job kind {kind!r}")
        job = self.store.add(kind, dict(payload), created_at=self.clock())
        self._enqueue(job)
        return job

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
        """Run the next queued job; return it, or None if the queue is empty."""
        while self._queue:
            _, _, job_id = heapq.heappop(self._queue)
            job = self.store.get(job_id)
            if job is None or job.status is not JobStatus.PENDING:
                continue
            return self._execute(job)
        return None

    def _execute(self, job: Job) -> Job:
        job.status = JobStatus.RUNNING
        self.store.save(job)
        handler = self.handlers[job.kind]
        try:
            result = handler(job.payload)
        except Exception:  # a BaseException is a crash and propagates
            job.status = JobStatus.FAILED
            job.result = None
        else:
            job.status = JobStatus.SUCCEEDED
            job.result = result
        self.store.save(job)
        return job
