from __future__ import annotations

from .leases import LeaseFence
from .models import JobStatus, RecoveryReport, TERMINAL_STATUSES
from .repositories import EventRepository, JobRepository


class RecoveryManager:
    def __init__(self, jobs: JobRepository, events: EventRepository) -> None:
        self._jobs = jobs
        self._events = events

    def release_expired_for_claim(self, *, now: float) -> tuple[str, ...]:
        released: list[str] = []
        for job in self._jobs.all():
            if (
                job.status is JobStatus.LEASED
                and job.lease_expires_at is not None
                and job.lease_expires_at <= now
            ):
                job.status = JobStatus.PENDING
                LeaseFence.clear(job)
                self._events.append("lease_expired", job, now=now)
                released.append(job.job_id)
        return tuple(released)

    def recover_after_restart(self, *, now: float) -> RecoveryReport:
        expired = held = terminal = 0
        runnable = 0
        recovered: list[str] = []
        for job in self._jobs.all():
            if job.status in TERMINAL_STATUSES:
                terminal += 1
                job.status = JobStatus.PENDING
                job.result = None
                job.last_error = None
            elif job.status is JobStatus.LEASED:
                if job.lease_expires_at is not None and job.lease_expires_at <= now:
                    expired += 1
                    job.status = JobStatus.PENDING
                    LeaseFence.clear(job)
                else:
                    held += 1
                    continue
            if job.status in {JobStatus.PENDING, JobStatus.RETRY_WAITING}:
                job.attempt = 0
                runnable += 1
                recovered.append(job.job_id)
                self._events.append("recovered", job, now=now)
        return RecoveryReport(
            expired_leases=expired,
            runnable_jobs=runnable,
            held_leases=held,
            terminal_jobs=terminal,
            recovered_job_ids=tuple(recovered),
        )
