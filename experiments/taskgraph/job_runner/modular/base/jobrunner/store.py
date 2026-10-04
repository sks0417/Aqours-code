"""SQLite persistence for jobs.

The table already has the progress columns ``attempts``, ``max_attempts``,
``next_run_at``, ``last_error``, and ``cancel_requested``. They can be read
with ``progress`` and written with ``update_progress`` or ``finish``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Job, JobStatus, Outcome

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_run_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
)
"""

PROGRESS_FIELDS = ("attempts", "max_attempts", "next_run_at", "last_error",
                   "cancel_requested")


class JobStore:
    """Jobs stored in one SQLite database file."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def add(self, kind: str, payload: dict[str, Any], created_at: float,
            max_attempts: int = 3) -> Job:
        """Insert a new pending job, due at ``created_at``, and return it."""
        cursor = self._conn.execute(
            "INSERT INTO jobs (kind, payload, status, created_at, max_attempts, "
            "next_run_at) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, json.dumps(payload), JobStatus.PENDING.value, created_at,
             max_attempts, created_at),
        )
        self._conn.commit()
        return self.get(cursor.lastrowid)

    def get(self, job_id: int) -> Job | None:
        """Return the job with ``job_id``, or None."""
        row = self._row(job_id)
        return self._row_to_job(row) if row is not None else None

    def list(self, status: JobStatus | None = None) -> list[Job]:
        """All jobs, or the jobs with ``status``, ordered by id."""
        return [self._row_to_job(row) for row in self.rows(status)]

    def rows(self, status: JobStatus | None = None) -> list[sqlite3.Row]:
        """Raw rows, ordered by id, for modules that need the progress columns."""
        if status is None:
            return self._conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return self._conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id", (status.value,)
        ).fetchall()

    def progress(self, job_id: int) -> dict[str, Any]:
        """The progress columns of one job."""
        row = self._row(job_id)
        return {name: row[name] for name in PROGRESS_FIELDS} if row is not None else {}

    def update_progress(self, job_id: int, **fields: Any) -> None:
        """Write progress columns (and ``status``) of one job."""
        allowed = {*PROGRESS_FIELDS, "status"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        if "status" in fields:
            fields["status"] = JobStatus(fields["status"]).value
        if "cancel_requested" in fields:
            fields["cancel_requested"] = int(bool(fields["cancel_requested"]))
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?",
                           (*fields.values(), job_id))
        self._conn.commit()

    def claim(self, job_id: int) -> Job:
        """Mark a job RUNNING just before its handler is called."""
        self._conn.execute("UPDATE jobs SET status = ? WHERE id = ?",
                           (JobStatus.RUNNING.value, job_id))
        self._conn.commit()
        return self.get(job_id)

    def finish(self, job_id: int, outcome: Outcome) -> Job:
        """Store the outcome of a run and return the job's new state."""
        fields: dict[str, Any] = {"status": outcome.status.value,
                                  "result": json.dumps(outcome.result),
                                  "last_error": outcome.error}
        if outcome.next_run_at is not None:
            fields["next_run_at"] = outcome.next_run_at
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?",
                           (*fields.values(), job_id))
        self._conn.commit()
        return self.get(job_id)

    def _row(self, job_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            status=JobStatus(row["status"]),
            result=json.loads(row["result"]) if row["result"] is not None else None,
            created_at=row["created_at"],
        )
