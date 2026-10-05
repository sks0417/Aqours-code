"""SQLite persistence for jobs.

The ``jobs`` table holds every job field. Other modules keep their own tables
in the same database: they pass the ``CREATE TABLE IF NOT EXISTS`` statement
to ``register_schema`` when they are imported, and ``create_tables`` (called
by ``Runner``) creates every registered table. ``execute`` and ``query`` run
SQL for those modules. ``transaction`` groups writes into one SQLite
transaction; outside it, every write is committed at once.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_run_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
)
"""

# Columns that ``add`` and ``update`` may set, with how each value is stored.
JOB_FIELDS: dict[str, Callable[[Any], Any]] = {
    "status": lambda value: JobStatus(value).value,
    "result": json.dumps,
    "attempts": int,
    "max_attempts": int,
    "next_run_at": float,
    "last_error": lambda value: value,
    "cancel_requested": lambda value: int(bool(value)),
}

_schemas: list[str] = []


def register_schema(statement: str) -> None:
    """Have ``create_tables`` run ``statement`` (a CREATE ... IF NOT EXISTS)."""
    if statement not in _schemas:
        _schemas.append(statement)


class JobStore:
    """Jobs, and the tables of other modules, in one SQLite database file."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._depth = 0
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def create_tables(self) -> None:
        """Create every table registered with ``register_schema``."""
        for statement in _schemas:
            self._conn.execute(statement)
        self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[JobStore]:
        """Commit every write inside the block together, or none on an error.

        Blocks may nest; the outermost one commits or rolls back.
        """
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._conn.rollback()
            raise
        self._depth -= 1
        if self._depth == 0:
            self._conn.commit()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Run one writing statement; committed now unless inside ``transaction``."""
        cursor = self._conn.execute(sql, params)
        if self._depth == 0:
            self._conn.commit()
        return cursor

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Run one reading statement and return its rows."""
        return self._conn.execute(sql, params).fetchall()

    def add(self, kind: str, payload: dict[str, Any], created_at: float,
            max_attempts: int = 3, **fields: Any) -> Job:
        """Insert a new pending job, due at ``created_at``, and return it.

        ``fields`` sets other columns of ``JOB_FIELDS``, such as ``next_run_at``.
        """
        with self.transaction():
            cursor = self.execute(
                "INSERT INTO jobs (kind, payload, status, created_at, max_attempts, "
                "next_run_at) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, json.dumps(payload), JobStatus.PENDING.value, created_at,
                 max_attempts, created_at),
            )
            self.update(cursor.lastrowid, **fields)
        return self.get(cursor.lastrowid)

    def get(self, job_id: int) -> Job | None:
        """Return the job with ``job_id``, or None."""
        rows = self.query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._row_to_job(rows[0]) if rows else None

    def list(self, status: JobStatus | None = None) -> list[Job]:
        """All jobs, or the jobs with ``status``, ordered by id."""
        if status is None:
            rows = self.query("SELECT * FROM jobs ORDER BY id")
        else:
            rows = self.query("SELECT * FROM jobs WHERE status = ? ORDER BY id",
                              (JobStatus(status).value,))
        return [self._row_to_job(row) for row in rows]

    def update(self, job_id: int, **fields: Any) -> None:
        """Write columns of one job. Status changes belong to ``transitions``."""
        unknown = set(fields) - set(JOB_FIELDS)
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [JOB_FIELDS[name](value) for name, value in fields.items()]
        self.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*values, job_id))

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
