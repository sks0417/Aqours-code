"""Job model, the outcome of one run, and state-change events."""
from __future__ import annotations

from dataclasses import dataclass
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
        }


@dataclass
class Outcome:
    """How a run ended: the state to store, plus any result or error.

    ``next_run_at`` is only stored when it is not None.
    """

    status: JobStatus
    result: Any = None
    error: str | None = None
    next_run_at: float | None = None


@dataclass(frozen=True)
class Event:
    """One state change of a job, published by ``jobrunner.transitions``.

    ``job`` is the job right after the change; ``old_status`` is None when
    the job was just created. ``reason`` and ``time`` follow SPEC.md "Audit
    log and statistics".
    """

    job: Job
    old_status: JobStatus | None
    new_status: JobStatus
    reason: str
    time: float
