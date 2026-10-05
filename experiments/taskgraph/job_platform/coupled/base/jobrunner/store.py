"""SQLite persistence for jobs."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Job, JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    created_at REAL NOT NULL
)
"""

# Progress columns added for retries, cancellation, and restart recovery.
PROGRESS_COLUMNS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 3",
    "next_run_at": "REAL NOT NULL DEFAULT 0",
    "last_error": "TEXT",
    "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
}


class JobStore:
    """Jobs stored in one SQLite database file."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add progress columns missing from databases created before them."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for name, definition in PROGRESS_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def add(self, kind: str, payload: dict[str, Any], created_at: float,
            max_attempts: int = 3) -> Job:
        """Insert a new pending job, due at ``created_at``, and return it."""
        cursor = self._conn.execute(
            "INSERT INTO jobs (kind, payload, status, result, created_at, attempts, "
            "max_attempts, next_run_at, last_error, cancel_requested) "
            "VALUES (?, ?, ?, NULL, ?, 0, ?, ?, NULL, 0)",
            (kind, json.dumps(payload), JobStatus.PENDING.value, created_at,
             max_attempts, created_at),
        )
        self._conn.commit()
        return Job(id=cursor.lastrowid, kind=kind, payload=payload,
                   status=JobStatus.PENDING, created_at=created_at,
                   max_attempts=max_attempts, next_run_at=created_at)

    def get(self, job_id: int) -> Job | None:
        """Return the job with ``job_id``, or None."""
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row is not None else None

    def save(self, job: Job) -> None:
        """Store the job's status, result, and progress fields."""
        self._conn.execute(
            "UPDATE jobs SET status = ?, result = ?, attempts = ?, max_attempts = ?, "
            "next_run_at = ?, last_error = ?, cancel_requested = ? WHERE id = ?",
            (job.status.value, json.dumps(job.result), job.attempts, job.max_attempts,
             job.next_run_at, job.last_error, int(job.cancel_requested), job.id),
        )
        self._conn.commit()

    def list(self, status: JobStatus | None = None) -> list[Job]:
        """All jobs, or the jobs with ``status``, ordered by id."""
        if status is None:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY id", (status.value,)
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            status=JobStatus(row["status"]),
            result=json.loads(row["result"]) if row["result"] is not None else None,
            created_at=row["created_at"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_run_at=row["next_run_at"],
            last_error=row["last_error"],
            cancel_requested=bool(row["cancel_requested"]),
        )
