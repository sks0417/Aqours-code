from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import Claim, Job, JobStatus, RecoveryReport


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "request_id": job.request_id,
        "task": job.task,
        "payload": deepcopy(job.payload),
        "max_attempts": job.max_attempts,
        "created_at": job.created_at,
        "status": job.status.value,
        "attempt": job.attempt,
        "lease_generation": job.lease_generation,
        "worker_id": job.worker_id,
        "lease_token": job.lease_token,
        "lease_expires_at": job.lease_expires_at,
        "retry_at": job.retry_at,
        "result": deepcopy(job.result),
        "last_error": job.last_error,
    }


def job_from_dict(value: dict[str, Any]) -> Job:
    data = deepcopy(value)
    data["status"] = JobStatus(data["status"])
    return Job(**data)


def claim_to_dict(claim: Claim) -> dict[str, Any]:
    return {
        "job_id": claim.job_id,
        "task": claim.task,
        "payload": deepcopy(claim.payload),
        "attempt": claim.attempt,
        "lease_generation": claim.lease_generation,
        "lease_token": claim.lease_token,
        "lease_expires_at": claim.lease_expires_at,
    }


def report_to_dict(report: RecoveryReport) -> dict[str, Any]:
    return {
        "expired_leases": report.expired_leases,
        "runnable_jobs": report.runnable_jobs,
        "held_leases": report.held_leases,
        "terminal_jobs": report.terminal_jobs,
        "recovered_job_ids": list(report.recovered_job_ids),
    }
