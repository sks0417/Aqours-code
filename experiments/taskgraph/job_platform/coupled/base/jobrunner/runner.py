"""Runs jobs one at a time from an in-memory queue backed by the store."""
from __future__ import annotations

import heapq
from collections.abc import Callable, Mapping
from typing import Any

from .errors import InvalidTransition, JobNotFound, TransientError
from .models import Job, JobStatus
from .store import JobStore

Handler = Callable[[dict[str, Any]], Any]

INTERRUPTED_ERROR = "interrupted by a process restart"


class Runner:
    """Executes jobs with registered handlers.

    Pending jobs wait in an in-memory queue ordered by ``next_run_at`` and
    then by id. ``_enqueue`` is the only way a job enters the queue: new
    jobs, retries, and jobs recovered after a restart all go through it.
    ``run_once`` takes the next due job out and runs it.
    """

    def __init__(self, store: JobStore, handlers: Mapping[str, Handler],
                 clock: Callable[[], float], max_attempts: int = 3,
                 base_delay: float = 1.0):
        self.store = store
        self.handlers = dict(handlers)
        self.clock = clock
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._queue: list[tuple[float, int]] = []
        self._recover()

    # ── queue ──

    def _recover(self) -> None:
        """Rebuild the queue from the store after a (re)start."""
        now = self.clock()
        for job in self.store.list(JobStatus.RUNNING):
            if job.cancel_requested:
                self._finish_cancelled(job)
            else:
                self._record_failure(job, INTERRUPTED_ERROR, now)
        self._queue.clear()  # rebuilt below from every pending job, recovered ones included
        for job in self.store.list(JobStatus.PENDING):
            self._enqueue(job)

    def _enqueue(self, job: Job, not_before: float | None = None) -> None:
        """Queue a pending job; with ``not_before``, delay it until then."""
        if not_before is not None:
            job.next_run_at = not_before
        heapq.heappush(self._queue, (job.next_run_at, job.id))

    def _next_due(self, now: float) -> Job | None:
        """Pop the next pending job whose time has come, skipping stale entries."""
        while self._queue and self._queue[0][0] <= now:
            due_at, job_id = heapq.heappop(self._queue)
            job = self.store.get(job_id)
            if (job is not None and job.status is JobStatus.PENDING
                    and job.next_run_at == due_at):
                return job
        return None

    def _backoff(self, failures: int) -> float:
        return self.base_delay * 2 ** (failures - 1)

    def _record_failure(self, job: Job, message: str, now: float) -> None:
        """Apply the retry rules after the ``job.attempts``-th failure."""
        job.last_error = message
        job.result = None
        if job.attempts < job.max_attempts:
            job.status = JobStatus.PENDING
            self._enqueue(job, not_before=now + self._backoff(job.attempts))
        else:
            job.status = JobStatus.FAILED
        self.store.save(job)

    def _finish_cancelled(self, job: Job) -> None:
        job.status = JobStatus.CANCELLED
        job.result = None
        self.store.save(job)

    # ── public interface ──

    def submit(self, kind: str, payload: dict[str, Any]) -> Job:
        """Create a pending job, due now, and queue it."""
        if kind not in self.handlers:
            raise ValueError(f"no handler for job kind {kind!r}")
        job = self.store.add(kind, dict(payload), created_at=self.clock(),
                             max_attempts=self.max_attempts)
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

    def cancel(self, job_id: int) -> Job:
        """Cancel a pending job now, or request cancellation of a running one."""
        job = self.get(job_id)
        if job.status is JobStatus.PENDING:
            self._finish_cancelled(job)
        elif job.status is JobStatus.RUNNING:
            job.cancel_requested = True
            self.store.save(job)
        else:
            raise InvalidTransition(f"job {job_id} is {job.status.value}")
        return job

    def run_once(self) -> Job | None:
        """Run the next due job; return it, or None if no job is due."""
        job = self._next_due(self.clock())
        if job is None:
            return None
        job.status = JobStatus.RUNNING
        job.attempts += 1
        self.store.save(job)
        handler = self.handlers[job.kind]
        try:
            result = handler(job.payload)
        except TransientError as exc:
            outcome: tuple[str, Any] = ("retry", str(exc))
        except Exception as exc:  # a BaseException is a crash and propagates
            outcome = ("fail", str(exc))
        else:
            outcome = ("ok", result)
        # The handler may have requested cancellation through another reference.
        current = self.get(job.id)
        if current.cancel_requested:
            self._finish_cancelled(current)
            return current
        kind, value = outcome
        if kind == "ok":
            current.status, current.result = JobStatus.SUCCEEDED, value
            self.store.save(current)
        elif kind == "retry":
            self._record_failure(current, value, self.clock())
        else:
            current.status, current.last_error, current.result = JobStatus.FAILED, value, None
            self.store.save(current)
        return current
