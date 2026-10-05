"""Job priorities and fair scheduling across tenants (SPEC.md "Priorities and fair scheduling").

Two scheduler orderings rank the jobs that can run: the highest ``priority``
first, then the tenant whose most recent start is the oldest. A transitions
subscriber records each tenant's turn when one of its jobs starts, in the
``tenant_turns`` table, so the rotation survives a restart. Registers
``POST /jobs/<id>/priority``, ``GET /queue``, ``GET /tenants``, and the
``/queue`` page.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import scheduler, transitions, web
from .errors import InvalidTransition, JobNotFound
from .models import Event, Job, JobStatus
from .store import JobStore, register_schema
from .validation import check_int

if TYPE_CHECKING:
    from .runner import Runner

register_schema("""CREATE TABLE IF NOT EXISTS tenant_turns (
    tenant TEXT PRIMARY KEY, turn INTEGER NOT NULL)""")


def by_priority(store: JobStore, job: Job) -> int:
    """Ordering: higher priorities first."""
    return -job.priority


def by_tenant_turn(store: JobStore, job: Job) -> int:
    """Ordering: the tenant that started a job longest ago first; -1 if never."""
    rows = store.query("SELECT turn FROM tenant_turns WHERE tenant = ?", (job.tenant,))
    return rows[0]["turn"] if rows else -1


scheduler.register_ordering("priority", 10, by_priority)
scheduler.register_ordering("tenant_turn", 20, by_tenant_turn)


def record_turn(store: JobStore, event: Event) -> None:
    """Subscriber: a start gives the job's tenant the newest turn."""
    if event.new_status is JobStatus.RUNNING:
        store.execute(
            "INSERT OR REPLACE INTO tenant_turns (tenant, turn) "
            "VALUES (?, (SELECT COALESCE(MAX(turn), 0) + 1 FROM tenant_turns))",
            (event.job.tenant,))


transitions.subscribe(record_turn)


def set_priority(store: JobStore, job_id: int, priority: int) -> Job:
    """Change the priority of a PENDING job."""
    check_int("priority", priority)
    job = store.get(job_id)
    if job is None:
        raise JobNotFound(job_id)
    if job.status is not JobStatus.PENDING:
        raise InvalidTransition(f"job {job_id} is {job.status.value}")
    return transitions.update(store, job_id, priority=priority)


def queue(store: JobStore) -> list[Job]:
    """Pending jobs by priority (highest first), then ``next_run_at``, then id."""
    return sorted(store.list(JobStatus.PENDING),
                  key=lambda job: (-job.priority, job.next_run_at, job.id))


def tenants(store: JobStore) -> list[dict[str, Any]]:
    """Pending, running, and finished jobs per tenant, ordered by tenant."""
    summary: dict[str, dict[str, Any]] = {}
    for job in store.list():
        row = summary.setdefault(job.tenant, {"tenant": job.tenant, "pending": 0,
                                              "running": 0, "finished": 0})
        row["finished" if job.status.is_final else job.status.value] += 1
    return [summary[tenant] for tenant in sorted(summary)]


@web.route("POST", r"/jobs/(?P<job_id>\d+)/priority")
def priority_route(runner: Runner, request: web.Request, job_id: str) -> web.Response:
    """``POST /jobs/<id>/priority`` with ``{"priority"}``: 200 and the job."""
    priority = web.fields(request, "priority")["priority"]
    return 200, runner.set_priority(int(job_id), priority).to_dict()


@web.route("GET", r"/queue")
def queue_route(runner: Runner, request: web.Request) -> web.Response:
    """``GET /queue``: 200 and the pending jobs in queue order."""
    return 200, {"jobs": [job.to_dict() for job in runner.queue()]}


@web.route("GET", r"/tenants")
def tenants_route(runner: Runner, request: web.Request) -> web.Response:
    """``GET /tenants``: 200 and the jobs per tenant."""
    return 200, {"tenants": runner.tenants()}


@web.page(r"/queue")
def queue_page(runner: Runner, request: web.Request) -> web.Page:
    """``/queue``: pending jobs in queue order, then the jobs per tenant."""
    rows = ([job.id, job.kind, job.tenant, job.priority, web.time(job.next_run_at)]
            for job in runner.queue())
    per_tenant = ([row["tenant"], row["pending"], row["running"], row["finished"]]
                  for row in runner.tenants())
    return 200, web.heading(
        "Queue",
        web.table("queue", ("ID", "Kind", "Tenant", "Priority", "Run at"), rows),
        web.table("tenants", ("Tenant", "Pending", "Running", "Finished"), per_tenant))
