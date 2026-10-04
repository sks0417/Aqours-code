"""Job model and the outcome of one run."""
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

    @property
    def is_final(self) -> bool:
        """True for states a job never leaves."""
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


@dataclass
class Job:
    """One unit of background work."""

    id: int
    kind: str
    payload: dict[str, Any]
    status: JobStatus = JobStatus.PENDING
    result: Any = None
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON representation used by the REST API."""
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": self.payload,
            "status": self.status.value,
            "result": self.result,
            "created_at": self.created_at,
        }


@dataclass
class Outcome:
    """How a run ended: the state to store, plus any result or error.

    ``JobStore.finish`` applies an outcome; ``next_run_at`` is only stored
    when it is not None.
    """

    status: JobStatus
    result: Any = None
    error: str | None = None
    next_run_at: float | None = None
