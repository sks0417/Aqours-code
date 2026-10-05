"""Runs jobs one at a time; the rules live in their own modules."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import (audit, cancellation, dependencies, notifications, ratelimit, recovery,
               recurring, retry, scheduler, transitions)
from . import priority as priorities
from .errors import JobNotFound
from .models import (AuditEntry, Job, JobStatus, Notification, Outcome, RateLimit,
                     RecurringJob, Subscription)
from .store import JobStore
from .validation import check_int, check_number, check_tenant

Handler = Callable[[dict[str, Any]], Any]


class Runner:
    """Executes jobs with registered handlers.

    ``run_once`` lets ``recurring`` create due jobs, asks
    ``scheduler.pick_next`` for a job, marks it RUNNING through
    ``transitions``, calls its handler, turns a failure into an outcome with
    ``retry.decide``, and stores the outcome with
    ``cancellation.finish_run``. ``recovery.recover`` runs once at startup.
    Every status change goes through ``transitions``, which tells its
    subscribers. The platform features each live in one module, and the
    methods below delegate to them.
    """

    def __init__(self, store: JobStore, handlers: Mapping[str, Handler],
                 clock: Callable[[], float], max_attempts: int = 3,
                 base_delay: float = 1.0):
        self.store = store
        self.handlers = dict(handlers)
        self.clock = clock
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        store.create_tables()
        recovery.recover(store, clock(), base_delay=base_delay)

    def check_kind(self, kind: Any) -> None:
        """Raise ``ValueError`` for a kind without a handler."""
        if kind not in self.handlers:
            raise ValueError(f"no handler for job kind {kind!r}")

    # ── jobs ──

    def submit(self, kind: str, payload: dict[str, Any], *, priority: int = 0,
               tenant: str = "default", run_at: float | None = None,
               depends_on: list[int] | tuple[int, ...] = ()) -> Job:
        """Create a pending job, due at ``run_at`` or now."""
        self.check_kind(kind)
        check_int("priority", priority)
        check_tenant(tenant)
        if run_at is not None:
            check_number("run_at", run_at)
        depends = dependencies.check_new(self.store, depends_on)
        now = self.clock()
        return transitions.create(self.store, kind, dict(payload), now=now,
                                  max_attempts=self.max_attempts, priority=priority,
                                  tenant=tenant, depends_on=depends,
                                  next_run_at=now if run_at is None else run_at)

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
        return cancellation.request_cancel(self.store, job_id, now=self.clock())

    def run_once(self) -> Job | None:
        """Run the next job; return its new state, or None if none can run."""
        now = self.clock()
        recurring.create_due(self.store, now, max_attempts=self.max_attempts)
        job = scheduler.pick_next(self.store, now)
        if job is None:
            return None
        job = transitions.change(self.store, job.id, JobStatus.RUNNING,
                                 reason=transitions.STARTED, now=now,
                                 attempts=job.attempts + 1)
        handler = self.handlers[job.kind]
        try:
            result = handler(job.payload)
        except Exception as exc:  # a BaseException is a crash and propagates
            outcome = retry.decide(job, exc, self.clock(), base_delay=self.base_delay)
        else:
            outcome = Outcome(status=JobStatus.SUCCEEDED, result=result)
        return cancellation.finish_run(self.store, job.id, outcome, now=self.clock())

    # ── priorities ──

    def set_priority(self, job_id: int, priority: int) -> Job:
        """Change the priority of a pending job."""
        return priorities.set_priority(self.store, job_id, priority)

    def queue(self) -> list[Job]:
        """Pending jobs by priority (highest first), then ``next_run_at``, then id."""
        return priorities.queue(self.store)

    def tenants(self) -> list[dict[str, Any]]:
        """Pending, running, and finished jobs per tenant, ordered by tenant."""
        return priorities.tenants(self.store)

    # ── recurring jobs ──

    def schedule_recurring(self, kind: str, payload: dict[str, Any], interval_s: float,
                           start_at: float | None = None, *, priority: int = 0,
                           tenant: str = "default") -> int:
        """Create a recurring definition and return its id."""
        self.check_kind(kind)
        return recurring.schedule(self.store, kind, payload, interval_s,
                                  self.clock() if start_at is None else start_at,
                                  priority=priority, tenant=tenant)

    def get_recurring(self, definition_id: int) -> RecurringJob:
        """Return one recurring definition."""
        return recurring.get(self.store, definition_id)

    def list_recurring(self) -> list[RecurringJob]:
        """Every recurring definition, ordered by id."""
        return recurring.list_all(self.store)

    def update_recurring(self, definition_id: int, *, payload: dict[str, Any] | None = None,
                         interval_s: float | None = None, priority: int | None = None,
                         tenant: str | None = None) -> RecurringJob:
        """Change the given fields of a definition; later jobs use the new values."""
        return recurring.update(self.store, definition_id, payload=payload,
                                interval_s=interval_s, priority=priority, tenant=tenant)

    def pause_recurring(self, definition_id: int) -> RecurringJob:
        """Stop a definition from creating jobs."""
        return recurring.set_paused(self.store, definition_id, True)

    def resume_recurring(self, definition_id: int) -> RecurringJob:
        """Let a paused definition create jobs again."""
        return recurring.set_paused(self.store, definition_id, False)

    def delete_recurring(self, definition_id: int) -> None:
        """Remove a definition; the jobs it created stay."""
        recurring.delete(self.store, definition_id)

    def recurring_jobs(self, definition_id: int) -> list[Job]:
        """The jobs a definition created, ordered by id."""
        return recurring.jobs_of(self.store, definition_id)

    # ── dependencies ──

    def dependents(self, job_id: int) -> list[Job]:
        """Jobs that list ``job_id`` in ``depends_on``, ordered by id."""
        return dependencies.dependents(self.store, job_id)

    # ── rate limits ──

    def set_rate_limit(self, kind: str, max_starts: int, window_s: float) -> RateLimit:
        """Allow at most ``max_starts`` starts of ``kind`` within ``window_s`` seconds."""
        self.check_kind(kind)
        return ratelimit.set_limit(self.store, kind, max_starts, window_s, now=self.clock())

    def clear_rate_limit(self, kind: str) -> None:
        """Remove the rate limit of ``kind``."""
        ratelimit.clear_limit(self.store, kind)

    def list_rate_limits(self) -> list[RateLimit]:
        """Every rate limit, ordered by kind."""
        return ratelimit.list_limits(self.store, now=self.clock())

    # ── webhook notifications ──

    def subscribe(self, url: str, kinds: list[str] | None = None,
                  statuses: list[str] | None = None) -> Subscription:
        """Register a webhook for later state changes."""
        return notifications.subscribe(self.store, url, kinds, statuses)

    def unsubscribe(self, subscription_id: int) -> None:
        """Remove a subscription; its pending notifications become dead."""
        notifications.unsubscribe(self.store, subscription_id)

    def list_subscriptions(self) -> list[Subscription]:
        """Every subscription, ordered by id."""
        return notifications.list_subscriptions(self.store)

    def list_notifications(self, status: str | None = None,
                           job_id: int | None = None) -> list[Notification]:
        """Outbox rows ordered by id, optionally filtered."""
        return notifications.list_notifications(self.store, status, job_id)

    def deliver_notifications(self, sender: Callable[[str, dict[str, Any]], Any]) -> int:
        """Try to deliver pending notifications; return how many were delivered."""
        return notifications.deliver(self.store, sender, now=self.clock())

    def retry_notification(self, notification_id: int) -> Notification:
        """Give a dead notification another round of delivery attempts."""
        return notifications.retry(self.store, notification_id, now=self.clock())

    # ── audit log and statistics ──

    def history(self, job_id: int) -> list[AuditEntry]:
        """The job's state changes in the order they happened."""
        return audit.history(self.store, job_id)

    def audit_log(self, since: float | None = None) -> list[AuditEntry]:
        """Every job's state changes in order; only those after ``since`` if given."""
        return audit.log(self.store, since)

    def stats(self, window_s: float | None = None) -> dict[str, Any]:
        """Counts by status and kind, and failure rate and attempts in the window."""
        return audit.stats(self.store, now=self.clock(), window_s=window_s)
