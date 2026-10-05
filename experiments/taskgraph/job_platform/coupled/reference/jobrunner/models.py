"""Job model and the other records of the job platform."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    """Lifecycle state of a job."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        """True for states a job never leaves."""
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)

    @property
    def can_cancel(self) -> bool:
        """True for states in which ``Runner.cancel`` is allowed."""
        return self in (JobStatus.PENDING, JobStatus.RUNNING)


@dataclass
class Job:
    """One unit of background work."""

    id: int
    kind: str
    payload: dict[str, Any]
    status: JobStatus = JobStatus.PENDING
    result: Any = None
    created_at: float = 0.0
    attempts: int = 0
    max_attempts: int = 3
    next_run_at: float = 0.0
    last_error: str | None = None
    cancel_requested: bool = False
    priority: int = 0
    tenant: str = "default"
    depends_on: list[int] = field(default_factory=list)
    definition_id: int | None = None
    cancel_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON representation used by the REST API."""
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": self.payload,
            "status": self.status.value,
            "result": self.result,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "next_run_at": self.next_run_at,
            "last_error": self.last_error,
            "cancel_requested": self.cancel_requested,
            "priority": self.priority,
            "tenant": self.tenant,
            "depends_on": list(self.depends_on),
            "definition_id": self.definition_id,
            "cancel_reason": self.cancel_reason,
        }


@dataclass
class RecurringJob:
    """A definition that creates a job every ``interval_s`` seconds."""

    id: int
    kind: str
    payload: dict[str, Any]
    interval_s: float
    next_run_at: float
    priority: int = 0
    tenant: str = "default"
    paused: bool = False
    last_job_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON representation used by the REST API."""
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": self.payload,
            "interval_s": self.interval_s,
            "next_run_at": self.next_run_at,
            "priority": self.priority,
            "tenant": self.tenant,
            "paused": self.paused,
            "last_job_id": self.last_job_id,
        }


@dataclass
class RateLimit:
    """At most ``max_starts`` starts of ``kind`` within ``window_s`` seconds."""

    kind: str
    max_starts: int
    window_s: float
    recent_starts: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON representation used by the REST API."""
        return {"kind": self.kind, "max_starts": self.max_starts,
                "window_s": self.window_s, "recent_starts": self.recent_starts}


@dataclass
class Subscription:
    """A webhook; ``None`` in ``kinds`` or ``statuses`` means all."""

    id: int
    url: str
    kinds: list[str] | None = None
    statuses: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON representation used by the REST API."""
        return {"id": self.id, "url": self.url, "kinds": self.kinds,
                "statuses": self.statuses}


@dataclass
class Notification:
    """One webhook call in the outbox."""

    id: int
    subscription_id: int
    url: str
    job_id: int
    payload: dict[str, Any]
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON representation used by the REST API."""
        return {"id": self.id, "subscription_id": self.subscription_id, "url": self.url,
                "job_id": self.job_id, "payload": self.payload, "status": self.status,
                "attempts": self.attempts, "next_attempt_at": self.next_attempt_at,
                "last_error": self.last_error}


@dataclass
class AuditEntry:
    """One state change of a job."""

    job_id: int
    old_status: str | None
    new_status: str
    time: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """JSON representation used by the REST API."""
        return {"job_id": self.job_id, "old_status": self.old_status,
                "new_status": self.new_status, "time": self.time, "reason": self.reason}
