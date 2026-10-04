"""HTML dashboard listing jobs."""
from __future__ import annotations

from collections.abc import Iterable
from html import escape

from .models import Job, JobStatus

COLUMNS = ("ID", "Kind", "Status", "Attempts", "Next retry", "Actions")


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
