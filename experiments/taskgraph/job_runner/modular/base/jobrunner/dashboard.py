"""HTML dashboard listing jobs."""
from __future__ import annotations

from collections.abc import Iterable
from html import escape

from .models import Job

COLUMNS = ("ID", "Kind", "Status", "Created")


def _row(job: Job) -> str:
    cells = (str(job.id), job.kind, job.status.value, f"{job.created_at:.1f}")
    return "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in cells) + "</tr>"


def render_jobs(jobs: Iterable[Job]) -> str:
    """Return an HTML table with one row per job."""
    header = "".join(f"<th>{escape(column)}</th>" for column in COLUMNS)
    rows = "\n".join(_row(job) for job in jobs)
    return (f'<table class="jobs">\n<thead><tr>{header}</tr></thead>\n'
            f"<tbody>\n{rows}\n</tbody>\n</table>")
