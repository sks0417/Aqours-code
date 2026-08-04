from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any

from .api import QueueAPI
from .errors import ValidationError
from .repositories import (
    EventRepository,
    JobRepository,
    OperationRepository,
    RequestRepository,
)
from .service import QueueService


@dataclass
class QueueApplication:
    api: QueueAPI
    jobs: JobRepository
    requests: RequestRepository
    events: EventRepository
    operations: OperationRepository
    lease_seconds: float

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "lease_seconds": self.lease_seconds,
            "jobs": self.jobs.snapshot(),
            "requests": self.requests.snapshot(),
            "events": self.events.snapshot(),
            "operations": self.operations.snapshot(),
        }


def build_application(snapshot: dict[str, Any] | None = None, *,
                      lease_seconds: float = 30.0) -> QueueApplication:
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)):
        raise ValidationError("lease_seconds must be a positive number")
    lease_seconds = float(lease_seconds)
    if not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValidationError("lease_seconds must be a positive number")
    data = deepcopy(snapshot or {})
    if data and data.get("version") != 1:
        raise ValidationError("unsupported snapshot version")
    if data:
        persisted_lease_seconds = data.get("lease_seconds")
        if (
            isinstance(persisted_lease_seconds, bool)
            or not isinstance(persisted_lease_seconds, (int, float))
            or not math.isfinite(float(persisted_lease_seconds))
            or float(persisted_lease_seconds) <= 0
        ):
            raise ValidationError("snapshot lease_seconds must be a positive number")
        lease_seconds = float(persisted_lease_seconds)
    jobs = JobRepository(data.get("jobs"))
    requests = RequestRepository(data.get("requests"))
    events = EventRepository(data.get("events"))
    operations = OperationRepository(data.get("operations"))
    service = QueueService(
        jobs, requests, events, operations, lease_seconds=lease_seconds
    )
    return QueueApplication(
        api=QueueAPI(service),
        jobs=jobs,
        requests=requests,
        events=events,
        operations=operations,
        lease_seconds=lease_seconds,
    )


def build_api(snapshot: dict[str, Any] | None = None, *,
              lease_seconds: float = 30.0) -> QueueAPI:
    return build_application(snapshot, lease_seconds=lease_seconds).api
