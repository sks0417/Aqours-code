"""HTML dashboard: the jobs table, and page dispatch.

Each module registers its own pages with ``jobrunner.web.page``; this module
registers the jobs page. Importing ``jobrunner.runner`` imports every module
that has pages.
"""
from __future__ import annotations

from collections.abc import Iterable

from . import web
from .models import Job, JobStatus
from .runner import Runner  # importing it loads every module's pages

COLUMNS = ("ID", "Kind", "Status", "Attempts", "Next retry", "Actions")


def _next_retry(job: Job) -> str:
    if job.status is JobStatus.PENDING and job.attempts > 0:
        return f"{job.next_run_at:.1f}"
    return "-"


def _actions(job: Job) -> web.Markup:
    if not job.status.can_cancel:
        return web.Markup("")
    return web.Markup(f'<form method="post" action="/jobs/{job.id}/cancel">'
                      '<button type="submit">Cancel</button></form>')


def render_jobs(jobs: Iterable[Job]) -> str:
    """Return an HTML table with one row per job."""
    rows = ([job.id, job.kind, job.status.value, f"{job.attempts}/{job.max_attempts}",
             _next_retry(job), _actions(job)] for job in jobs)
    return web.table("jobs", COLUMNS, rows)


@web.page(r"/jobs")
def jobs_page(runner: Runner, request: web.Request) -> web.Page:
    """``/jobs``: every job."""
    return 200, render_jobs(runner.list())


def render_page(runner: Runner, path: str) -> web.Page:
    """Return ``(status_code, html)`` for the dashboard page at ``path``."""
    return web.render(runner, path)
