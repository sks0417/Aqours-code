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

    def add(self, kind: str, payload: dict[str, Any], created_at: float) -> Job:
        """Insert a new pending job and return it."""
        cursor = self._conn.execute(
            "INSERT INTO jobs (kind, payload, status, result, created_at) "
            "VALUES (?, ?, ?, NULL, ?)",
            (kind, json.dumps(payload), JobStatus.PENDING.value, created_at),
        )
        self._conn.commit()
        return Job(id=cursor.lastrowid, kind=kind, payload=payload,
                   status=JobStatus.PENDING, created_at=created_at)

    def get(self, job_id: int) -> Job | None:
        """Return the job with ``job_id``, or None."""
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row is not None else None

    def save(self, job: Job) -> None:
        """Store the job's status and result."""
        self._conn.execute(
            "UPDATE jobs SET status = ?, result = ? WHERE id = ?",
            (job.status.value, json.dumps(job.result), job.id),
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
        )
