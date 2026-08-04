from __future__ import annotations

from copy import deepcopy
from typing import Any

from .errors import JobNotFound
from .models import Job
from .serialization import job_from_dict, job_to_dict


class JobRepository:
    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        snapshot = deepcopy(snapshot or {})
        self._jobs = {
            row["job_id"]: job_from_dict(row)
            for row in snapshot.get("jobs", [])
        }
        self._next_job_id = int(snapshot.get("next_job_id", 1))
        self._next_lease_id = int(snapshot.get("next_lease_id", 1))

    def create(self, *, request_id: str, task: str, payload: dict[str, Any],
               max_attempts: int, created_at: float) -> Job:
        job_id = f"job-{self._next_job_id:06d}"
        self._next_job_id += 1
        job = Job(
            job_id=job_id,
            request_id=request_id,
            task=task,
            payload=deepcopy(payload),
            max_attempts=max_attempts,
            created_at=created_at,
        )
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise JobNotFound(job_id) from exc

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def allocate_lease_token(self, job: Job) -> str:
        token = f"{job.job_id}:g{job.lease_generation}:l{self._next_lease_id}"
        self._next_lease_id += 1
        return token

    def snapshot(self) -> dict[str, Any]:
        return {
            "jobs": [job_to_dict(job) for job in self._jobs.values()],
            "next_job_id": self._next_job_id,
            "next_lease_id": self._next_lease_id,
        }


class RequestRepository:
    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self._bindings = deepcopy(snapshot or {})

    def find(self, request_id: str) -> dict[str, str] | None:
        binding = self._bindings.get(request_id)
        return deepcopy(binding) if binding is not None else None

    def bind(self, request_id: str, fingerprint: str, job_id: str) -> None:
        self._bindings[request_id] = {
            "fingerprint": fingerprint,
            "job_id": job_id,
        }

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._bindings)


class EventRepository:
    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        snapshot = deepcopy(snapshot or {})
        self._events = list(snapshot.get("events", []))
        self._next_sequence = int(snapshot.get("next_sequence", 1))

    def append(self, event_type: str, job: Job, *, now: float,
               details: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "sequence": self._next_sequence,
            "event_type": event_type,
            "job_id": job.job_id,
            "at": now,
            "status": job.status.value,
            "attempt": job.attempt,
            "details": deepcopy(details or {}),
        }
        self._next_sequence += 1
        self._events.append(event)
        return deepcopy(event)

    def list(self, job_id: str | None = None) -> list[dict[str, Any]]:
        events = self._events
        if job_id is not None:
            events = [event for event in events if event["job_id"] == job_id]
        return deepcopy(events)

    def snapshot(self) -> dict[str, Any]:
        return {
            "events": deepcopy(self._events),
            "next_sequence": self._next_sequence,
        }


class OperationRepository:
    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self._receipts = deepcopy(snapshot or {})

    @staticmethod
    def _key(job_id: str, lease_token: str, operation: str) -> str:
        return f"{job_id}\0{lease_token}\0{operation}"

    def find(self, job_id: str, lease_token: str,
             operation: str) -> dict[str, Any] | None:
        receipt = self._receipts.get(self._key(job_id, lease_token, operation))
        return deepcopy(receipt) if receipt is not None else None

    def record(self, job_id: str, lease_token: str, operation: str,
               fingerprint: str) -> None:
        self._receipts[self._key(job_id, lease_token, operation)] = {
            "fingerprint": fingerprint,
        }

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._receipts)
