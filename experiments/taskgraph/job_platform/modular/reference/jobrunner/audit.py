"""Audit log and statistics (SPEC.md "Audit log and statistics").

A transitions subscriber writes one ``audit_log`` row per state change, in
the transaction of the change. ``history`` reads a job's rows, ``log`` the
rows of all jobs; ``stats``
counts jobs by status and kind and computes the failure rate and average
attempts of the jobs that finished within a window. Registers
``GET /jobs/<id>/history``, ``GET /audit``, and ``GET /stats`` and their pages.
"""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from . import transitions, web
from .errors import JobNotFound
from .models import AuditEntry, Event, JobStatus
from .store import JobStore, register_schema
from .validation import check_number

if TYPE_CHECKING:
    from .runner import Runner

register_schema("""CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, old_status TEXT,
    new_status TEXT NOT NULL, time REAL NOT NULL, reason TEXT NOT NULL)""")

FINISHED = (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value)


def record(store: JobStore, event: Event) -> None:
    """Subscriber: one audit row per state change."""
    store.execute(
        "INSERT INTO audit_log (job_id, old_status, new_status, time, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (event.job.id, event.old_status.value if event.old_status else None,
         event.new_status.value, event.time, event.reason))


transitions.subscribe(record)


def history(store: JobStore, job_id: int) -> list[AuditEntry]:
    """The job's state changes in the order they happened."""
    if store.get(job_id) is None:
        raise JobNotFound(job_id)
    rows = store.query("SELECT * FROM audit_log WHERE job_id = ? ORDER BY id", (job_id,))
    return [AuditEntry(row["job_id"], row["old_status"], row["new_status"], row["time"],
                       row["reason"]) for row in rows]


def log(store: JobStore, since: float | None = None) -> list[AuditEntry]:
    """Every job's state changes in order; only those after ``since`` if given."""
    if since is None:
        rows = store.query("SELECT * FROM audit_log ORDER BY id")
    else:
        check_number("since", since)
        rows = store.query("SELECT * FROM audit_log WHERE time > ? ORDER BY id", (since,))
    return [AuditEntry(row["job_id"], row["old_status"], row["new_status"], row["time"],
                       row["reason"]) for row in rows]


def stats(store: JobStore, *, now: float, window_s: float | None = None) -> dict[str, Any]:
    """The ``stats()`` dict of SPEC.md for the window ``(now - window_s, now]``."""
    if window_s is not None:
        check_number("window_s", window_s, positive=True)
    jobs = store.list()
    by_status = {status.value: 0 for status in JobStatus}
    for job in jobs:
        by_status[job.status.value] += 1
    since = float("-inf") if window_s is None else now - window_s
    rows = store.query(
        "SELECT audit_log.new_status AS status, jobs.attempts AS attempts FROM audit_log "
        "JOIN jobs ON jobs.id = audit_log.job_id "
        "WHERE audit_log.new_status IN (?, ?) AND audit_log.time > ? AND audit_log.time <= ?",
        (*FINISHED, since, now))
    failed = sum(1 for row in rows if row["status"] == JobStatus.FAILED.value)
    return {
        "window_s": window_s,
        "by_status": by_status,
        "by_kind": dict(sorted(Counter(job.kind for job in jobs).items())),
        "by_tenant": dict(sorted(Counter(job.tenant for job in jobs).items())),
        "finished": len(rows),
        "failed": failed,
        "failure_rate": failed / len(rows) if rows else 0.0,
        "average_attempts": sum(row["attempts"] for row in rows) / len(rows) if rows else 0.0,
    }


def _query_number(request: web.Request, name: str) -> float | None:
    value = request.query.get(name)
    return None if value is None else web.number(value, name)


@web.route("GET", r"/jobs/(?P<job_id>\d+)/history")
def history_route(runner: Runner, request: web.Request, job_id: str) -> web.Response:
    """``GET /jobs/<id>/history``: 200 and the job's audit entries."""
    entries = runner.history(int(job_id))
    return 200, {"job_id": int(job_id), "history": [entry.to_dict() for entry in entries]}


@web.route("GET", r"/stats")
def stats_route(runner: Runner, request: web.Request) -> web.Response:
    """``GET /stats[?window_s=...]``: 200 and the ``stats()`` dict."""
    return 200, runner.stats(_query_number(request, "window_s"))


@web.route("GET", r"/audit")
def audit_route(runner: Runner, request: web.Request) -> web.Response:
    """``GET /audit[?since=...]``: 200 and the entries of all jobs."""
    entries = runner.audit_log(_query_number(request, "since"))
    return 200, {"entries": [entry.to_dict() for entry in entries]}


@web.page(r"/jobs/(?P<job_id>\d+)/history")
def history_page(runner: Runner, request: web.Request, job_id: str) -> web.Page:
    """``/jobs/<id>/history``."""
    rows = ([web.time(entry.time), entry.old_status or "-", entry.new_status, entry.reason]
            for entry in runner.history(int(job_id)))
    return 200, web.heading(f"Job {int(job_id)} history", web.table(
        "history", ("Time", "From", "To", "Reason"), rows))


@web.page(r"/audit")
def audit_page(runner: Runner, request: web.Request) -> web.Page:
    """``/audit[?since=...]``."""
    rows = ([web.time(entry.time), entry.job_id, entry.old_status or "-", entry.new_status,
             entry.reason] for entry in runner.audit_log(_query_number(request, "since")))
    return 200, web.heading("Audit log", web.table(
        "audit", ("Time", "Job", "From", "To", "Reason"), rows))


@web.page(r"/stats")
def stats_page(runner: Runner, request: web.Request) -> web.Page:
    """``/stats[?window_s=...]``."""
    stats_ = runner.stats(_query_number(request, "window_s"))
    summary = [["Finished", stats_["finished"]], ["Failed", stats_["failed"]],
               ["Failure rate", f"{stats_['failure_rate']:.1%}"],
               ["Average attempts", f"{stats_['average_attempts']:.2f}"]]
    return 200, web.heading(
        "Statistics",
        web.table("stats-status", ("Status", "Count"), stats_["by_status"].items()),
        web.table("stats-kind", ("Kind", "Count"), stats_["by_kind"].items()),
        web.table("stats-tenant", ("Tenant", "Count"), stats_["by_tenant"].items()),
        web.table("stats-summary", ("Metric", "Value"), summary))
