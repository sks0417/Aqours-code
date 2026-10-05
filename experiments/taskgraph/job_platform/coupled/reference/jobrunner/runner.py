"""Runs jobs one at a time and implements every feature of the job platform."""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from .errors import InvalidTransition, JobNotFound, NotFound, TransientError
from .models import (AuditEntry, Job, JobStatus, Notification, RateLimit, RecurringJob,
                     Subscription)
from .store import JobStore

Handler = Callable[[dict[str, Any]], Any]
Sender = Callable[[str, dict[str, Any]], Any]

INTERRUPTED_ERROR = "interrupted by a process restart"
CANCEL_REQUESTED = "cancel_requested"
DEPENDENCY_FAILED = "dependency_failed"
NOTIFICATION_STATUSES = ("pending", "delivered", "dead")
MAX_DELIVERY_ATTEMPTS = 5


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class Runner:
    """Executes jobs with registered handlers.

    ``run_once`` first creates the jobs of due recurring definitions, then
    picks the job to run among the pending ones (``_next_job``: due,
    dependencies succeeded, under the kind's rate limit; highest priority,
    then the tenant that waited longest, then ``next_run_at`` and id). Every
    state change is saved together with its audit entry and outbox rows
    (``_changed``) in one transaction.
    """

    def __init__(self, store: JobStore, handlers: Mapping[str, Handler],
                 clock: Callable[[], float], max_attempts: int = 3,
                 base_delay: float = 1.0):
        self.store = store
        self.handlers = dict(handlers)
        self.clock = clock
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._recover()

    # ── validation ──

    def _check_kind(self, kind: Any) -> None:
        if kind not in self.handlers:
            raise ValueError(f"no handler for job kind {kind!r}")

    @staticmethod
    def _check_priority(priority: Any) -> None:
        if not _is_int(priority):
            raise ValueError("priority must be an int")

    @staticmethod
    def _check_tenant(tenant: Any) -> None:
        if not isinstance(tenant, str) or not tenant:
            raise ValueError("tenant must be a non-empty string")

    def _check_dependencies(self, depends_on: Any) -> list[int]:
        if not isinstance(depends_on, (list, tuple)):
            raise ValueError("depends_on must be a list of job ids")
        ids: list[int] = []
        for job_id in depends_on:
            if not _is_int(job_id):
                raise ValueError("depends_on must be a list of job ids")
            if self.store.get(job_id) is None:
                raise ValueError(f"dependency {job_id} does not exist")
            if job_id not in ids:
                ids.append(job_id)
        return ids

    # ── recovery ──

    def _recover(self) -> None:
        """Repair jobs that were RUNNING when the previous process stopped."""
        now = self.clock()
        for job in self.store.list(JobStatus.RUNNING):
            if job.cancel_requested:
                self._finish_cancelled(job, CANCEL_REQUESTED, now)
            else:
                self._record_failure(job, INTERRUPTED_ERROR, now)

    # ── choosing the next job ──

    def _can_run(self, job: Job, now: float, limits: dict[str, RateLimit]) -> bool:
        if job.next_run_at > now:
            return False
        for dependency_id in job.depends_on:
            dependency = self.store.get(dependency_id)
            if dependency is None or dependency.status is not JobStatus.SUCCEEDED:
                return False
        limit = limits.get(job.kind)
        if limit is not None:
            started = self.store.count_starts(job.kind, now - limit.window_s, now)
            if started >= limit.max_starts:
                return False
        return True

    def _next_job(self, now: float) -> Job | None:
        """The pending job to run at ``now``, or None."""
        limits = self.store.rate_limits()
        turns = self.store.tenant_turns()
        candidates = [job for job in self.store.list(JobStatus.PENDING)
                      if self._can_run(job, now, limits)]
        return min(candidates, default=None,
                   key=lambda job: (-job.priority, turns.get(job.tenant, -1),
                                    job.next_run_at, job.id))

    # ── state changes ──

    def _changed(self, job: Job, old_status: JobStatus | None, reason: str,
                 now: float) -> None:
        """Audit, notify, and cascade one state change; call inside a transaction."""
        self.store.add_audit(job.id, old_status, job.status, now, reason)
        payload = {"job_id": job.id, "kind": job.kind, "tenant": job.tenant,
                   "old_status": old_status.value if old_status is not None else None,
                   "new_status": job.status.value, "reason": reason, "time": now}
        for subscription in self.store.list_subscriptions():
            if subscription.kinds is not None and job.kind not in subscription.kinds:
                continue
            if subscription.statuses is not None and job.status.value not in subscription.statuses:
                continue
            self.store.add_notification(subscription, job.id,
                                        {"subscription_id": subscription.id, **payload}, now)
        if job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
            for dependent in self.store.list(JobStatus.PENDING):
                if job.id in dependent.depends_on:
                    self._finish_cancelled(dependent, DEPENDENCY_FAILED, now)

    def _create(self, kind: str, payload: dict[str, Any], now: float, *, priority: int,
                tenant: str, next_run_at: float | None = None,
                depends_on: list[int] | None = None,
                definition_id: int | None = None) -> Job:
        with self.store.transaction():
            job = self.store.add(kind, dict(payload), created_at=now,
                                 max_attempts=self.max_attempts, priority=priority,
                                 tenant=tenant, next_run_at=next_run_at,
                                 depends_on=depends_on, definition_id=definition_id)
            self._changed(job, None, "submitted", now)
            for dependency_id in depends_on or []:
                if self.get(dependency_id).status in (JobStatus.FAILED, JobStatus.CANCELLED):
                    self._finish_cancelled(job, DEPENDENCY_FAILED, now)
                    break
        return self.get(job.id)

    def _backoff(self, failures: int) -> float:
        return self.base_delay * 2 ** (failures - 1)

    def _record_failure(self, job: Job, message: str, now: float) -> None:
        """Apply the retry rules after the ``job.attempts``-th failure."""
        old_status = job.status
        job.last_error = message
        job.result = None
        if job.attempts < job.max_attempts:
            job.status = JobStatus.PENDING
            job.next_run_at = now + self._backoff(job.attempts)
        else:
            job.status = JobStatus.FAILED
        with self.store.transaction():
            self.store.save(job)
            self._changed(job, old_status, message, now)

    def _finish_cancelled(self, job: Job, reason: str, now: float) -> None:
        old_status = job.status
        job.status = JobStatus.CANCELLED
        job.result = None
        job.cancel_reason = reason
        with self.store.transaction():
            self.store.save(job)
            self._changed(job, old_status, reason, now)

    # ── jobs ──

    def submit(self, kind: str, payload: dict[str, Any], *, priority: int = 0,
               tenant: str = "default", run_at: float | None = None,
               depends_on: list[int] | tuple[int, ...] = ()) -> Job:
        """Create a pending job, due at ``run_at`` or now."""
        self._check_kind(kind)
        self._check_priority(priority)
        self._check_tenant(tenant)
        if run_at is not None and not _is_number(run_at):
            raise ValueError("run_at must be a number")
        dependencies = self._check_dependencies(depends_on)
        return self._create(kind, payload, self.clock(), priority=priority, tenant=tenant,
                            next_run_at=run_at, depends_on=dependencies)

    def get(self, job_id: int) -> Job:
        """Return the job with ``job_id``."""
        job = self.store.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    def list(self, status: JobStatus | None = None) -> list[Job]:
        """Jobs ordered by id, optionally filtered by status."""
        return self.store.list(status)

    def cancel(self, job_id: int) -> Job:
        """Cancel a pending job now, or request cancellation of a running one."""
        job = self.get(job_id)
        if job.status is JobStatus.PENDING:
            self._finish_cancelled(job, CANCEL_REQUESTED, self.clock())
        elif job.status is JobStatus.RUNNING:
            job.cancel_requested = True
            self.store.save(job)
        else:
            raise InvalidTransition(f"job {job_id} is {job.status.value}")
        return self.get(job_id)

    def run_once(self) -> Job | None:
        """Run the next job; return it, or None if no job can run."""
        now = self.clock()
        self._create_recurring_jobs(now)
        job = self._next_job(now)
        if job is None:
            return None
        job.status = JobStatus.RUNNING
        job.attempts += 1
        with self.store.transaction():
            self.store.save(job)
            self.store.record_start(job, now)
            self._changed(job, JobStatus.PENDING, "started", now)
        handler = self.handlers[job.kind]
        try:
            result = handler(job.payload)
        except TransientError as exc:
            outcome: tuple[str, Any] = ("retry", str(exc))
        except Exception as exc:  # a BaseException is a crash and propagates
            outcome = ("fail", str(exc))
        else:
            outcome = ("ok", result)
        now = self.clock()
        # The handler may have requested cancellation through another reference.
        current = self.get(job.id)
        if current.cancel_requested:
            self._finish_cancelled(current, CANCEL_REQUESTED, now)
            return self.get(job.id)
        kind, value = outcome
        if kind == "ok":
            current.status, current.result = JobStatus.SUCCEEDED, value
            with self.store.transaction():
                self.store.save(current)
                self._changed(current, JobStatus.RUNNING, "succeeded", now)
        elif kind == "retry":
            self._record_failure(current, value, now)
        else:
            current.status, current.last_error, current.result = JobStatus.FAILED, value, None
            with self.store.transaction():
                self.store.save(current)
                self._changed(current, JobStatus.RUNNING, value, now)
        return self.get(job.id)

    # ── priorities ──

    def set_priority(self, job_id: int, priority: int) -> Job:
        """Change the priority of a pending job."""
        self._check_priority(priority)
        job = self.get(job_id)
        if job.status is not JobStatus.PENDING:
            raise InvalidTransition(f"job {job_id} is {job.status.value}")
        job.priority = priority
        self.store.save(job)
        return self.get(job_id)

    def queue(self) -> list[Job]:
        """Pending jobs by priority (highest first), then ``next_run_at``, then id."""
        return sorted(self.store.list(JobStatus.PENDING),
                      key=lambda job: (-job.priority, job.next_run_at, job.id))

    def tenants(self) -> list[dict[str, Any]]:
        """Pending, running, and finished jobs per tenant, ordered by tenant."""
        summary: dict[str, dict[str, Any]] = {}
        for job in self.store.list():
            row = summary.setdefault(job.tenant, {"tenant": job.tenant, "pending": 0,
                                                  "running": 0, "finished": 0})
            key = "finished" if job.status.is_final else job.status.value
            row[key] += 1
        return [summary[tenant] for tenant in sorted(summary)]

    # ── recurring jobs ──

    @staticmethod
    def _check_interval(interval_s: Any) -> None:
        if not _is_number(interval_s) or interval_s <= 0:
            raise ValueError("interval_s must be a positive number")

    @staticmethod
    def _check_payload(payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")

    def schedule_recurring(self, kind: str, payload: dict[str, Any], interval_s: float,
                           start_at: float | None = None, *, priority: int = 0,
                           tenant: str = "default") -> int:
        """Create a recurring definition and return its id."""
        self._check_kind(kind)
        self._check_interval(interval_s)
        if start_at is not None and not _is_number(start_at):
            raise ValueError("start_at must be a number")
        self._check_priority(priority)
        self._check_tenant(tenant)
        self._check_payload(payload)
        start = self.clock() if start_at is None else start_at
        return self.store.add_recurring(kind, dict(payload), interval_s, start, priority,
                                        tenant).id

    def get_recurring(self, definition_id: int) -> RecurringJob:
        """Return one recurring definition."""
        definition = self.store.get_recurring(definition_id)
        if definition is None:
            raise NotFound(f"recurring definition {definition_id} not found")
        return definition

    def list_recurring(self) -> list[RecurringJob]:
        """Every recurring definition, ordered by id."""
        return self.store.list_recurring()

    def update_recurring(self, definition_id: int, *, payload: dict[str, Any] | None = None,
                         interval_s: float | None = None, priority: int | None = None,
                         tenant: str | None = None) -> RecurringJob:
        """Change the given fields of a definition; later jobs use the new values."""
        definition = self.get_recurring(definition_id)
        if payload is not None:
            self._check_payload(payload)
            definition.payload = dict(payload)
        if interval_s is not None:
            self._check_interval(interval_s)
            definition.interval_s = interval_s
        if priority is not None:
            self._check_priority(priority)
            definition.priority = priority
        if tenant is not None:
            self._check_tenant(tenant)
            definition.tenant = tenant
        self.store.save_recurring(definition)
        return definition

    def pause_recurring(self, definition_id: int) -> RecurringJob:
        """Stop a definition from creating jobs."""
        definition = self.get_recurring(definition_id)
        definition.paused = True
        self.store.save_recurring(definition)
        return definition

    def resume_recurring(self, definition_id: int) -> RecurringJob:
        """Let a paused definition create jobs again."""
        definition = self.get_recurring(definition_id)
        definition.paused = False
        self.store.save_recurring(definition)
        return definition

    def delete_recurring(self, definition_id: int) -> None:
        """Remove a definition; the jobs it created stay."""
        self.get_recurring(definition_id)
        self.store.delete_recurring(definition_id)

    def recurring_jobs(self, definition_id: int) -> list[Job]:
        """The jobs a definition created, ordered by id."""
        self.get_recurring(definition_id)
        return [job for job in self.store.list() if job.definition_id == definition_id]

    def _create_recurring_jobs(self, now: float) -> None:
        for definition in self.store.list_recurring():
            if definition.paused or definition.next_run_at > now:
                continue
            last = (self.store.get(definition.last_job_id)
                    if definition.last_job_id is not None else None)
            if last is None or last.status.is_final:
                job = self._create(definition.kind, definition.payload, now,
                                   priority=definition.priority, tenant=definition.tenant,
                                   definition_id=definition.id)
                definition.last_job_id = job.id
            periods = math.floor((now - definition.next_run_at) / definition.interval_s) + 1
            definition.next_run_at += periods * definition.interval_s
            while definition.next_run_at <= now:
                definition.next_run_at += definition.interval_s
            self.store.save_recurring(definition)

    # ── dependencies ──

    def dependents(self, job_id: int) -> list[Job]:
        """Jobs that list ``job_id`` in ``depends_on``, ordered by id."""
        self.get(job_id)
        return [job for job in self.store.list() if job_id in job.depends_on]

    # ── rate limits ──

    def _with_recent_starts(self, limit: RateLimit, now: float) -> RateLimit:
        limit.recent_starts = self.store.count_starts(limit.kind, now - limit.window_s, now)
        return limit

    def set_rate_limit(self, kind: str, max_starts: int, window_s: float) -> RateLimit:
        """Allow at most ``max_starts`` starts of ``kind`` within ``window_s`` seconds."""
        self._check_kind(kind)
        if not _is_int(max_starts) or max_starts < 1:
            raise ValueError("max_starts must be an int of at least 1")
        if not _is_number(window_s) or window_s <= 0:
            raise ValueError("window_s must be a positive number")
        self.store.set_rate_limit(kind, max_starts, window_s)
        return self._with_recent_starts(RateLimit(kind, max_starts, window_s), self.clock())

    def clear_rate_limit(self, kind: str) -> None:
        """Remove the rate limit of ``kind``."""
        if not self.store.delete_rate_limit(kind):
            raise NotFound(f"no rate limit for {kind!r}")

    def list_rate_limits(self) -> list[RateLimit]:
        """Every rate limit, ordered by kind."""
        now = self.clock()
        return [self._with_recent_starts(limit, now)
                for limit in self.store.rate_limits().values()]

    # ── webhook notifications ──

    def subscribe(self, url: str, kinds: list[str] | None = None,
                  statuses: list[str] | None = None) -> Subscription:
        """Register a webhook for later state changes."""
        if not isinstance(url, str) or not url:
            raise ValueError("url must be a non-empty string")
        if kinds is not None:
            if not isinstance(kinds, (list, tuple)) or not all(isinstance(k, str) for k in kinds):
                raise ValueError("kinds must be a list of strings")
            kinds = list(kinds)
        if statuses is not None:
            if not isinstance(statuses, (list, tuple)):
                raise ValueError("statuses must be a list of statuses")
            try:
                statuses = [JobStatus(status).value for status in statuses]
            except ValueError as exc:
                raise ValueError(f"unknown status in {statuses!r}") from exc
        return self.store.add_subscription(url, kinds, statuses)

    def unsubscribe(self, subscription_id: int) -> None:
        """Remove a subscription; its pending notifications become dead."""
        if not self.store.delete_subscription(subscription_id):
            raise NotFound(f"subscription {subscription_id} not found")

    def list_subscriptions(self) -> list[Subscription]:
        """Every subscription, ordered by id."""
        return self.store.list_subscriptions()

    def list_notifications(self, status: str | None = None,
                           job_id: int | None = None) -> list[Notification]:
        """Outbox rows ordered by id, optionally filtered."""
        if status is not None and status not in NOTIFICATION_STATUSES:
            raise ValueError(f"unknown notification status {status!r}")
        return self.store.list_notifications(status, job_id)

    def retry_notification(self, notification_id: int) -> Notification:
        """Give a dead notification another round of delivery attempts."""
        note = self.store.get_notification(notification_id)
        if note is None:
            raise NotFound(f"notification {notification_id} not found")
        if note.status != "dead":
            raise InvalidTransition(f"notification {notification_id} is {note.status}")
        if note.subscription_id not in {sub.id for sub in self.store.list_subscriptions()}:
            raise InvalidTransition(f"subscription {note.subscription_id} was removed")
        note.status, note.attempts, note.next_attempt_at = "pending", 0, self.clock()
        self.store.save_notification(note)
        return note

    def deliver_notifications(self, sender: Sender) -> int:
        """Try to deliver pending notifications; return how many were delivered."""
        now = self.clock()
        blocked: set[tuple[int, int]] = set()
        delivered = 0
        for note in self.store.list_notifications("pending"):
            group = (note.subscription_id, note.job_id)
            if group in blocked:
                continue
            if note.next_attempt_at > now:
                blocked.add(group)
                continue
            try:
                sender(note.url, note.payload)
            except Exception as exc:  # a BaseException is a crash and propagates
                note.attempts += 1
                note.last_error = str(exc)
                if note.attempts >= MAX_DELIVERY_ATTEMPTS:
                    note.status = "dead"
                else:
                    note.next_attempt_at = now + 2 ** (note.attempts - 1)
                    blocked.add(group)
            else:
                note.attempts += 1
                note.status = "delivered"
                delivered += 1
            self.store.save_notification(note)
        return delivered

    # ── audit log and statistics ──

    def history(self, job_id: int) -> list[AuditEntry]:
        """The job's state changes in the order they happened."""
        self.get(job_id)
        return self.store.audit_entries(job_id)

    def audit_log(self, since: float | None = None) -> list[AuditEntry]:
        """Every job's state changes in order; only those after ``since`` if given."""
        if since is not None and not _is_number(since):
            raise ValueError("since must be a number")
        return self.store.audit_entries(since=since)

    def stats(self, window_s: float | None = None) -> dict[str, Any]:
        """Counts by status and kind, and failure rate and attempts in the window."""
        if window_s is not None and (not _is_number(window_s) or window_s <= 0):
            raise ValueError("window_s must be a positive number")
        now = self.clock()
        jobs = self.store.list()
        by_status = {status.value: 0 for status in JobStatus}
        for job in jobs:
            by_status[job.status.value] += 1
        by_kind = dict(sorted(Counter(job.kind for job in jobs).items()))
        by_tenant = dict(sorted(Counter(job.tenant for job in jobs).items()))
        finished = [entry for entry in self.store.audit_entries()
                    if entry.new_status in ("succeeded", "failed") and entry.time <= now
                    and (window_s is None or entry.time > now - window_s)]
        failed = sum(1 for entry in finished if entry.new_status == "failed")
        attempts = [self.get(entry.job_id).attempts for entry in finished]
        return {
            "window_s": window_s,
            "by_status": by_status,
            "by_kind": by_kind,
            "by_tenant": by_tenant,
            "finished": len(finished),
            "failed": failed,
            "failure_rate": failed / len(finished) if finished else 0.0,
            "average_attempts": sum(attempts) / len(attempts) if attempts else 0.0,
        }
