"""HTML dashboard: the jobs table and the pages of the job platform."""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import NotFound
from .models import Job, JobStatus, Notification
from .runner import Runner

COLUMNS = ("ID", "Kind", "Status", "Attempts", "Next retry", "Actions")

_HISTORY_PAGE = re.compile(r"^/jobs/(\d+)/history$")
_DEPENDENCIES_PAGE = re.compile(r"^/jobs/(\d+)/dependencies$")


class _Raw(str):
    """A table cell that is already HTML."""


def _next_retry(job: Job) -> str:
    if job.status is JobStatus.PENDING and job.attempts > 0:
        return f"{job.next_run_at:.1f}"
    return "-"


def _actions(job: Job) -> str:
    if not job.status.can_cancel:
        return ""
    return (f'<form method="post" action="/jobs/{job.id}/cancel">'
            '<button type="submit">Cancel</button></form>')


def _row(job: Job) -> str:
    cells = (str(job.id), job.kind, job.status.value,
             f"{job.attempts}/{job.max_attempts}", _next_retry(job))
    text = "".join(f"<td>{escape(cell)}</td>" for cell in cells)
    return f"<tr>{text}<td>{_actions(job)}</td></tr>"


def render_jobs(jobs: Iterable[Job]) -> str:
    """Return an HTML table with one row per job."""
    header = "".join(f"<th>{escape(column)}</th>" for column in COLUMNS)
    rows = "\n".join(_row(job) for job in jobs)
    return (f'<table class="jobs">\n<thead><tr>{header}</tr></thead>\n'
            f"<tbody>\n{rows}\n</tbody>\n</table>")


def _table(css_class: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    def cell(value: Any) -> str:
        return value if isinstance(value, _Raw) else escape(str(value))

    header = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body = "\n".join("<tr>" + "".join(f"<td>{cell(value)}</td>" for value in row) + "</tr>"
                     for row in rows)
    return (f'<table class="{css_class}">\n<thead><tr>{header}</tr></thead>\n'
            f"<tbody>\n{body}\n</tbody>\n</table>")


def _page(title: str, *tables: str) -> str:
    return "\n".join((f"<h1>{escape(title)}</h1>", *tables))


def _queue_page(runner: Runner) -> str:
    rows = ([job.id, job.kind, job.tenant, job.priority, f"{job.next_run_at:.1f}"]
            for job in runner.queue())
    tenants = ([row["tenant"], row["pending"], row["running"], row["finished"]]
               for row in runner.tenants())
    return _page("Queue", _table("queue", ("ID", "Kind", "Tenant", "Priority", "Run at"), rows),
                 _table("tenants", ("Tenant", "Pending", "Running", "Finished"), tenants))


def _recurring_page(runner: Runner) -> str:
    rows = []
    for definition in runner.list_recurring():
        action, label = ("resume", "Resume") if definition.paused else ("pause", "Pause")
        form = _Raw(f'<form method="post" action="/recurring/{definition.id}/{action}">'
                    f'<button type="submit">{label}</button></form>')
        rows.append([definition.id, definition.kind, f"{definition.interval_s:.1f}",
                     f"{definition.next_run_at:.1f}",
                     "paused" if definition.paused else "active",
                     definition.last_job_id if definition.last_job_id is not None else "-",
                     form])
    columns = ("ID", "Kind", "Interval", "Next run", "State", "Last job", "Actions")
    return _page("Recurring jobs", _table("recurring", columns, rows))


def _dependencies_page(runner: Runner, job_id: int) -> str:
    job = runner.get(job_id)
    rows = [["depends on", dep.id, dep.kind, dep.status.value]
            for dep in (runner.get(dep_id) for dep_id in job.depends_on)]
    rows += [["dependent", dep.id, dep.kind, dep.status.value]
             for dep in runner.dependents(job_id)]
    return _page(f"Job {job_id} dependencies",
                 _table("dependencies", ("Relation", "ID", "Kind", "Status"), rows))


def _rate_limits_page(runner: Runner) -> str:
    rows = ([limit.kind, limit.max_starts, f"{limit.window_s:.1f}", limit.recent_starts]
            for limit in runner.list_rate_limits())
    columns = ("Kind", "Max starts", "Window", "Recent starts")
    return _page("Rate limits", _table("rate-limits", columns, rows))


def _notifications_page(runner: Runner) -> str:
    def listed(values: list[str] | None) -> str:
        return "all" if values is None else ", ".join(values)

    subscribed = runner.list_subscriptions()
    live = {sub.id for sub in subscribed}

    def actions(note: Notification) -> _Raw:
        if note.status != "dead" or note.subscription_id not in live:
            return _Raw("")
        return _Raw(f'<form method="post" action="/notifications/{note.id}/retry">'
                    '<button type="submit">Retry</button></form>')

    subscriptions = ([sub.id, sub.url, listed(sub.kinds), listed(sub.statuses)]
                     for sub in subscribed)
    outbox = ([note.id, note.subscription_id, note.job_id, note.payload["new_status"],
               note.status, note.attempts,
               f"{note.next_attempt_at:.1f}" if note.status == "pending" else "-",
               actions(note)]
              for note in runner.list_notifications())
    return _page("Notifications",
                 _table("subscriptions", ("ID", "URL", "Kinds", "Statuses"), subscriptions),
                 _table("outbox", ("ID", "Subscription", "Job", "Event", "Status", "Attempts",
                                   "Next attempt", "Actions"), outbox))


def _history_page(runner: Runner, job_id: int) -> str:
    rows = ([f"{entry.time:.1f}", entry.old_status or "-", entry.new_status, entry.reason]
            for entry in runner.history(job_id))
    return _page(f"Job {job_id} history",
                 _table("history", ("Time", "From", "To", "Reason"), rows))


def _query_number(query: dict[str, list[str]], name: str) -> float | None:
    value = query.get(name, [None])[0]
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


def _audit_page(runner: Runner, query: dict[str, list[str]]) -> str:
    rows = ([f"{entry.time:.1f}", entry.job_id, entry.old_status or "-", entry.new_status,
             entry.reason] for entry in runner.audit_log(_query_number(query, "since")))
    return _page("Audit log", _table("audit", ("Time", "Job", "From", "To", "Reason"), rows))


def _stats_page(runner: Runner, query: dict[str, list[str]]) -> str:
    stats = runner.stats(_query_number(query, "window_s"))
    summary = [["Finished", stats["finished"]], ["Failed", stats["failed"]],
               ["Failure rate", f"{stats['failure_rate']:.1%}"],
               ["Average attempts", f"{stats['average_attempts']:.2f}"]]
    return _page("Statistics",
                 _table("stats-status", ("Status", "Count"), stats["by_status"].items()),
                 _table("stats-kind", ("Kind", "Count"), stats["by_kind"].items()),
                 _table("stats-tenant", ("Tenant", "Count"), stats["by_tenant"].items()),
                 _table("stats-summary", ("Metric", "Value"), summary))


def render_page(runner: Runner, path: str) -> tuple[int, str]:
    """Return ``(status_code, html)`` for the dashboard page at ``path``."""
    parts = urlsplit(path)
    page = parts.path
    try:
        if page == "/jobs":
            return 200, render_jobs(runner.list())
        if page == "/queue":
            return 200, _queue_page(runner)
        if page == "/recurring":
            return 200, _recurring_page(runner)
        if page == "/rate-limits":
            return 200, _rate_limits_page(runner)
        if page == "/notifications":
            return 200, _notifications_page(runner)
        if page == "/stats":
            return 200, _stats_page(runner, parse_qs(parts.query))
        if page == "/audit":
            return 200, _audit_page(runner, parse_qs(parts.query))
        match = _DEPENDENCIES_PAGE.match(page)
        if match:
            return 200, _dependencies_page(runner, int(match.group(1)))
        match = _HISTORY_PAGE.match(page)
        if match:
            return 200, _history_page(runner, int(match.group(1)))
    except NotFound as exc:
        return 404, f"<p>Not found: {escape(str(exc))}</p>"
    except ValueError as exc:
        return 400, f"<p>Error: {escape(str(exc))}</p>"
    return 404, f"<p>Not found: {escape(page)}</p>"
