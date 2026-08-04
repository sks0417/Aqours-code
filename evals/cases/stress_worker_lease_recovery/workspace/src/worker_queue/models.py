from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAITING = "retry_waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}


@dataclass
class Job:
    job_id: str
    request_id: str
    task: str
    payload: dict[str, Any]
    max_attempts: int
    created_at: float
    status: JobStatus = JobStatus.PENDING
    attempt: int = 0
    lease_generation: int = 0
    worker_id: str | None = None
    lease_token: str | None = None
    lease_expires_at: float | None = None
    retry_at: float | None = None
    result: Any = None
    last_error: str | None = None


@dataclass(frozen=True)
class Claim:
    job_id: str
    task: str
    payload: dict[str, Any]
    attempt: int
    lease_generation: int
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True)
class RecoveryReport:
    expired_leases: int = 0
    runnable_jobs: int = 0
    held_leases: int = 0
    terminal_jobs: int = 0
    recovered_job_ids: tuple[str, ...] = field(default_factory=tuple)
