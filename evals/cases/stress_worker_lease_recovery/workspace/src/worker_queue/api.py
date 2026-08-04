from __future__ import annotations

from typing import Any

from .serialization import claim_to_dict, job_to_dict, report_to_dict
from .service import QueueService


class QueueAPI:
    def __init__(self, service: QueueService) -> None:
        self._service = service

    def submit(self, request: object, *, request_id: object,
               now: object) -> dict[str, Any]:
        return job_to_dict(self._service.submit(request, request_id=request_id, now=now))

    def claim(self, worker_id: object, *, now: object) -> dict[str, Any] | None:
        claim = self._service.claim(worker_id, now=now)
        return None if claim is None else claim_to_dict(claim)

    def complete(self, job_id: str, lease_token: str, result: Any, *,
                 now: object) -> dict[str, Any]:
        return job_to_dict(
            self._service.complete(job_id, lease_token, result, now=now)
        )

    def fail(self, job_id: str, lease_token: str, error: object, *,
             retry_at: object, now: object) -> dict[str, Any]:
        return job_to_dict(
            self._service.fail(
                job_id, lease_token, error, retry_at=retry_at, now=now
            )
        )

    def cancel(self, job_id: str, *, now: object) -> dict[str, Any]:
        return job_to_dict(self._service.cancel(job_id, now=now))

    def recover(self, *, now: object) -> dict[str, Any]:
        return report_to_dict(self._service.recover(now=now))

    def get_job(self, job_id: str) -> dict[str, Any]:
        return job_to_dict(self._service.jobs.get(job_id))

    def list_jobs(self) -> list[dict[str, Any]]:
        return [job_to_dict(job) for job in self._service.jobs.all()]

    def history(self, job_id: str | None = None) -> list[dict[str, Any]]:
        if job_id is not None:
            self._service.jobs.get(job_id)
        return self._service.events.list(job_id)
