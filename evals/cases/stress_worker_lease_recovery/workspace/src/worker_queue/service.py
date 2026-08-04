from __future__ import annotations

from copy import deepcopy
from typing import Any

from .errors import IdempotencyConflict, InvalidStateTransition, OperationConflict
from .fingerprint import operation_fingerprint, request_fingerprint
from .leases import LeaseFence
from .models import Claim, Job, JobStatus, RecoveryReport, TERMINAL_STATUSES
from .recovery import RecoveryManager
from .repositories import (
    EventRepository,
    JobRepository,
    OperationRepository,
    RequestRepository,
)
from .validation import (
    normalize_identifier,
    normalize_json_value,
    normalize_request,
    normalize_time,
)


class QueueService:
    def __init__(self, jobs: JobRepository, requests: RequestRepository,
                 events: EventRepository, operations: OperationRepository,
                 *, lease_seconds: float) -> None:
        self.jobs = jobs
        self.requests = requests
        self.events = events
        self.operations = operations
        self.leases = LeaseFence(jobs, lease_seconds=lease_seconds)
        self.recovery = RecoveryManager(jobs, events)

    def submit(self, request: object, *, request_id: object, now: object) -> Job:
        normalized = normalize_request(request)
        key = normalize_identifier(request_id, field="request_id")
        submitted_at = normalize_time(now, field="now")
        fingerprint = request_fingerprint(normalized)
        existing = self.requests.find(key)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise IdempotencyConflict(f"request_id {key!r} has different content")
            return self.jobs.get(existing["job_id"])
        job = self.jobs.create(
            request_id=key,
            task=normalized["task"],
            payload=normalized["payload"],
            max_attempts=normalized["max_attempts"],
            created_at=submitted_at,
        )
        self.requests.bind(key, fingerprint, job.job_id)
        self.events.append("submitted", job, now=submitted_at)
        return job

    def claim(self, worker_id: object, *, now: object) -> Claim | None:
        worker = normalize_identifier(worker_id, field="worker_id")
        claimed_at = normalize_time(now, field="now")
        self.recovery.release_expired_for_claim(now=claimed_at)
        for job in self.jobs.all():
            runnable = job.status is JobStatus.PENDING or (
                job.status is JobStatus.RETRY_WAITING
                and job.retry_at is not None
                and job.retry_at <= claimed_at
            )
            if not runnable:
                continue
            claim = self.leases.issue(job, worker_id=worker, now=claimed_at)
            self.events.append(
                "claimed", job, now=claimed_at,
                details={"worker_id": worker, "lease_generation": job.lease_generation},
            )
            return claim
        return None

    def complete(self, job_id: str, lease_token: str, result: Any,
                 *, now: object) -> Job:
        completed_at = normalize_time(now, field="now")
        job = self.jobs.get(job_id)
        normalized_result = normalize_json_value(result, field="result")
        fingerprint = operation_fingerprint({"result": normalized_result})
        receipt = self.operations.find(job_id, lease_token, "complete")
        if receipt is not None:
            if receipt["fingerprint"] != fingerprint:
                raise OperationConflict("completion retry changed its result")
            return job
        self.leases.assert_current(job, lease_token, now=completed_at)
        job.status = JobStatus.COMPLETED
        job.result = deepcopy(normalized_result)
        self.operations.record(job_id, lease_token, "complete", fingerprint)
        self.events.append("completed", job, now=completed_at)
        self.leases.clear(job)
        return job

    def fail(self, job_id: str, lease_token: str, error: object, *,
             retry_at: object, now: object) -> Job:
        failed_at = normalize_time(now, field="now")
        retry_time = normalize_time(retry_at, field="retry_at")
        message = normalize_identifier(error, field="error")
        job = self.jobs.get(job_id)
        fingerprint = operation_fingerprint({"error": message, "retry_at": retry_time})
        receipt = self.operations.find(job_id, lease_token, "fail")
        if receipt is not None:
            if receipt["fingerprint"] != fingerprint:
                raise OperationConflict("failure retry changed its content")
            return job
        self.leases.assert_current(job, lease_token, now=failed_at)
        job.attempt += 1
        job.last_error = message
        if job.attempt >= job.max_attempts:
            job.status = JobStatus.FAILED
            job.retry_at = None
            self.operations.record(job_id, lease_token, "fail", fingerprint)
            event_type = "failed"
        else:
            job.status = JobStatus.RETRY_WAITING
            job.retry_at = retry_time
            event_type = "retry_scheduled"
        self.events.append(event_type, job, now=failed_at)
        self.leases.clear(job)
        return job

    def cancel(self, job_id: str, *, now: object) -> Job:
        cancelled_at = normalize_time(now, field="now")
        job = self.jobs.get(job_id)
        if job.status is JobStatus.CANCELLED:
            return job
        job.status = JobStatus.CANCELLED
        job.retry_at = None
        self.events.append("cancelled", job, now=cancelled_at)
        return job

    def recover(self, *, now: object) -> RecoveryReport:
        return self.recovery.recover_after_restart(
            now=normalize_time(now, field="now")
        )
