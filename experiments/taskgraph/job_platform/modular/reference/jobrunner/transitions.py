"""Every change of a job's status goes through this module.

``create`` inserts a new job and ``change`` moves a job to another status.
Each publishes an ``Event`` to every subscriber (``subscribe``) inside the
same SQLite transaction as the change, so whatever a subscriber writes (an
audit row, say) is committed together with it. A subscriber may itself
change jobs; those events are published after every subscriber has seen the
current one, so all subscribers see all events in the same order.
``update`` writes other job fields without an event.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from .errors import InvalidTransition, JobNotFound
from .models import Event, Job, JobStatus
from .store import JobStore

Listener = Callable[[JobStore, Event], None]

SUBMITTED = "submitted"
STARTED = "started"
SUCCEEDED = "succeeded"

# Allowed status changes; None is "not created yet".
ALLOWED: dict[JobStatus | None, frozenset[JobStatus]] = {
    None: frozenset({JobStatus.PENDING}),
    JobStatus.PENDING: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED,
                                  JobStatus.PENDING, JobStatus.CANCELLED}),
}

_listeners: list[Listener] = []
_queue: deque[tuple[JobStore, Event]] = deque()
_publishing = False


def subscribe(listener: Listener) -> None:
    """Call ``listener(store, event)`` for every later state change."""
    if listener not in _listeners:
        _listeners.append(listener)


def create(store: JobStore, kind: str, payload: dict[str, Any], *, now: float,
           max_attempts: int, **fields: Any) -> Job:
    """Insert a PENDING job created at ``now`` and publish its creation.

    Returns the job as it is once every subscriber has run.
    """
    with store.transaction():
        job = store.add(kind, payload, created_at=now, max_attempts=max_attempts, **fields)
        _publish(store, Event(job, None, JobStatus.PENDING, SUBMITTED, now))
    return store.get(job.id)


def change(store: JobStore, job_id: int, status: JobStatus, *, reason: str, now: float,
           **fields: Any) -> Job:
    """Move a job to ``status``, write ``fields`` with it, and publish the change.

    Raises ``JobNotFound`` for an unknown job and ``InvalidTransition`` for a
    change that ``ALLOWED`` does not list. A job that becomes CANCELLED gets
    ``cancel_reason = reason``.
    """
    if status is JobStatus.CANCELLED:
        fields.setdefault("cancel_reason", reason)
    with store.transaction():
        job = store.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        if status not in ALLOWED.get(job.status, frozenset()):
            raise InvalidTransition(f"job {job_id} is {job.status.value}")
        store.update(job_id, status=status, **fields)
        _publish(store, Event(store.get(job_id), job.status, status, reason, now))
    return store.get(job_id)


def update(store: JobStore, job_id: int, **fields: Any) -> Job:
    """Write job fields other than the status; no event is published."""
    if "status" in fields:
        raise ValueError("use change() to change a job's status")
    if store.get(job_id) is None:
        raise JobNotFound(job_id)
    store.update(job_id, **fields)
    return store.get(job_id)


def _publish(store: JobStore, event: Event) -> None:
    """Deliver ``event``, and the events its subscribers cause, in order."""
    global _publishing
    _queue.append((store, event))
    if _publishing:
        return
    _publishing = True
    try:
        while _queue:
            target, current = _queue.popleft()
            for listener in list(_listeners):
                listener(target, current)
    finally:
        _publishing = False
        _queue.clear()
