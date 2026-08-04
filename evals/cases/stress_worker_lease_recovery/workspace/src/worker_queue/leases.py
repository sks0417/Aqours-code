from __future__ import annotations

from copy import deepcopy

from .errors import StaleLease
from .models import Claim, Job, JobStatus
from .repositories import JobRepository


class LeaseFence:
    def __init__(self, jobs: JobRepository, *, lease_seconds: float) -> None:
        self._jobs = jobs
        self._lease_seconds = lease_seconds

    def issue(self, job: Job, *, worker_id: str, now: float) -> Claim:
        job.status = JobStatus.LEASED
        job.attempt += 1
        job.lease_generation += 1
        job.worker_id = worker_id
        job.lease_token = self._jobs.allocate_lease_token(job)
        job.lease_expires_at = now + self._lease_seconds
        job.retry_at = None
        return Claim(
            job_id=job.job_id,
            task=job.task,
            payload=deepcopy(job.payload),
            attempt=job.attempt,
            lease_generation=job.lease_generation,
            lease_token=job.lease_token,
            lease_expires_at=job.lease_expires_at,
        )

    def assert_current(self, job: Job, lease_token: str, *, now: float) -> None:
        token_is_for_job = isinstance(lease_token, str) and lease_token.startswith(
            f"{job.job_id}:"
        )
        if job.status is not JobStatus.LEASED or not token_is_for_job:
            raise StaleLease(f"lease is no longer current for {job.job_id}")

    @staticmethod
    def clear(job: Job) -> None:
        job.worker_id = None
        job.lease_token = None
        job.lease_expires_at = None
