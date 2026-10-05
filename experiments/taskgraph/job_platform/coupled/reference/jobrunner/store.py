"""SQLite persistence for jobs and the records of the job platform."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import AuditEntry, Job, JobStatus, Notification, RateLimit, RecurringJob, Subscription

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

# Progress columns added for retries, cancellation, and restart recovery,
# then the platform columns.
PROGRESS_COLUMNS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 3",
    "next_run_at": "REAL NOT NULL DEFAULT 0",
    "last_error": "TEXT",
    "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "tenant": "TEXT NOT NULL DEFAULT 'default'",
    "depends_on": "TEXT NOT NULL DEFAULT '[]'",
    "definition_id": "INTEGER",
    "cancel_reason": "TEXT",
}

PLATFORM_TABLES = (
    """CREATE TABLE IF NOT EXISTS recurring (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL,
        interval_s REAL NOT NULL, next_run_at REAL NOT NULL, priority INTEGER NOT NULL,
        tenant TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0, last_job_id INTEGER)""",
    """CREATE TABLE IF NOT EXISTS rate_limits (
        kind TEXT PRIMARY KEY, max_starts INTEGER NOT NULL, window_s REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS job_starts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, kind TEXT NOT NULL,
        tenant TEXT NOT NULL, time REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, kinds TEXT,
        statuses TEXT)""",
    """CREATE TABLE IF NOT EXISTS outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id INTEGER NOT NULL,
        url TEXT NOT NULL, job_id INTEGER NOT NULL, payload TEXT NOT NULL,
        status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL, last_error TEXT)""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, old_status TEXT,
        new_status TEXT NOT NULL, time REAL NOT NULL, reason TEXT NOT NULL)""",
)


def _json_or_none(text: str | None) -> Any:
    return json.loads(text) if text is not None else None


class JobStore:
    """Jobs stored in one SQLite database file."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._depth = 0
        self._conn.execute(SCHEMA)
        self._migrate()
        for statement in PLATFORM_TABLES:
            self._conn.execute(statement)
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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit every write inside the block together; blocks may nest."""
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._conn.rollback()
            raise
        self._depth -= 1
        if self._depth == 0:
            self._conn.commit()

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, params)
        if self._depth == 0:
            self._conn.commit()
        return cursor

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    # ── jobs ──

    def add(self, kind: str, payload: dict[str, Any], created_at: float,
            max_attempts: int = 3, *, priority: int = 0, tenant: str = "default",
            next_run_at: float | None = None, depends_on: list[int] | None = None,
            definition_id: int | None = None) -> Job:
        """Insert a new pending job and return it; due at ``created_at`` by default."""
        due = created_at if next_run_at is None else next_run_at
        cursor = self._write(
            "INSERT INTO jobs (kind, payload, status, result, created_at, attempts, "
            "max_attempts, next_run_at, last_error, cancel_requested, priority, tenant, "
            "depends_on, definition_id, cancel_reason) "
            "VALUES (?, ?, ?, NULL, ?, 0, ?, ?, NULL, 0, ?, ?, ?, ?, NULL)",
            (kind, json.dumps(payload), JobStatus.PENDING.value, created_at,
             max_attempts, due, priority, tenant, json.dumps(depends_on or []),
             definition_id),
        )
        return self.get(cursor.lastrowid)

    def get(self, job_id: int) -> Job | None:
        """Return the job with ``job_id``, or None."""
        rows = self._rows("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._row_to_job(rows[0]) if rows else None

    def save(self, job: Job) -> None:
        """Store the job's status, result, and progress fields."""
        self._write(
            "UPDATE jobs SET status = ?, result = ?, attempts = ?, max_attempts = ?, "
            "next_run_at = ?, last_error = ?, cancel_requested = ?, priority = ?, "
            "cancel_reason = ? WHERE id = ?",
            (job.status.value, json.dumps(job.result), job.attempts, job.max_attempts,
             job.next_run_at, job.last_error, int(job.cancel_requested), job.priority,
             job.cancel_reason, job.id),
        )

    def list(self, status: JobStatus | None = None) -> list[Job]:
        """All jobs, or the jobs with ``status``, ordered by id."""
        if status is None:
            rows = self._rows("SELECT * FROM jobs ORDER BY id")
        else:
            rows = self._rows("SELECT * FROM jobs WHERE status = ? ORDER BY id",
                              (status.value,))
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
            priority=row["priority"],
            tenant=row["tenant"],
            depends_on=json.loads(row["depends_on"]),
            definition_id=row["definition_id"],
            cancel_reason=row["cancel_reason"],
        )

    # ── starts: rate limits and tenant turns ──

    def record_start(self, job: Job, time: float) -> None:
        """Remember that ``job`` started at ``time``."""
        self._write("INSERT INTO job_starts (job_id, kind, tenant, time) VALUES (?, ?, ?, ?)",
                    (job.id, job.kind, job.tenant, time))

    def count_starts(self, kind: str, after: float, until: float) -> int:
        """Starts of ``kind`` with ``after < time <= until``."""
        rows = self._rows("SELECT COUNT(*) AS n FROM job_starts "
                          "WHERE kind = ? AND time > ? AND time <= ?", (kind, after, until))
        return rows[0]["n"]

    def tenant_turns(self) -> dict[str, int]:
        """For each tenant, the sequence number of its most recent start."""
        rows = self._rows("SELECT tenant, MAX(id) AS turn FROM job_starts GROUP BY tenant")
        return {row["tenant"]: row["turn"] for row in rows}

    def set_rate_limit(self, kind: str, max_starts: int, window_s: float) -> None:
        self._write("INSERT OR REPLACE INTO rate_limits (kind, max_starts, window_s) "
                    "VALUES (?, ?, ?)", (kind, max_starts, window_s))

    def delete_rate_limit(self, kind: str) -> bool:
        return self._write("DELETE FROM rate_limits WHERE kind = ?", (kind,)).rowcount > 0

    def rate_limits(self) -> dict[str, RateLimit]:
        """Every rate limit by kind, ordered by kind (``recent_starts`` left at 0)."""
        rows = self._rows("SELECT * FROM rate_limits ORDER BY kind")
        return {row["kind"]: RateLimit(row["kind"], row["max_starts"], row["window_s"])
                for row in rows}

    # ── recurring definitions ──

    def add_recurring(self, kind: str, payload: dict[str, Any], interval_s: float,
                      next_run_at: float, priority: int, tenant: str) -> RecurringJob:
        cursor = self._write(
            "INSERT INTO recurring (kind, payload, interval_s, next_run_at, priority, tenant) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, json.dumps(payload), interval_s, next_run_at, priority, tenant))
        return self.get_recurring(cursor.lastrowid)

    def get_recurring(self, definition_id: int) -> RecurringJob | None:
        rows = self._rows("SELECT * FROM recurring WHERE id = ?", (definition_id,))
        return self._row_to_recurring(rows[0]) if rows else None

    def list_recurring(self) -> list[RecurringJob]:
        return [self._row_to_recurring(row)
                for row in self._rows("SELECT * FROM recurring ORDER BY id")]

    def save_recurring(self, definition: RecurringJob) -> None:
        self._write("UPDATE recurring SET payload = ?, interval_s = ?, next_run_at = ?, "
                    "priority = ?, tenant = ?, paused = ?, last_job_id = ? WHERE id = ?",
                    (json.dumps(definition.payload), definition.interval_s,
                     definition.next_run_at, definition.priority, definition.tenant,
                     int(definition.paused), definition.last_job_id, definition.id))

    def delete_recurring(self, definition_id: int) -> None:
        self._write("DELETE FROM recurring WHERE id = ?", (definition_id,))

    @staticmethod
    def _row_to_recurring(row: sqlite3.Row) -> RecurringJob:
        return RecurringJob(
            id=row["id"], kind=row["kind"], payload=json.loads(row["payload"]),
            interval_s=row["interval_s"],
            next_run_at=row["next_run_at"], priority=row["priority"], tenant=row["tenant"],
            paused=bool(row["paused"]), last_job_id=row["last_job_id"])

    # ── webhooks ──

    def add_subscription(self, url: str, kinds: list[str] | None,
                         statuses: list[str] | None) -> Subscription:
        cursor = self._write("INSERT INTO subscriptions (url, kinds, statuses) VALUES (?, ?, ?)",
                             (url, None if kinds is None else json.dumps(kinds),
                              None if statuses is None else json.dumps(statuses)))
        return Subscription(cursor.lastrowid, url, kinds, statuses)

    def list_subscriptions(self) -> list[Subscription]:
        return [Subscription(row["id"], row["url"], _json_or_none(row["kinds"]),
                             _json_or_none(row["statuses"]))
                for row in self._rows("SELECT * FROM subscriptions ORDER BY id")]

    def delete_subscription(self, subscription_id: int) -> bool:
        """Remove a subscription; its pending notifications become dead."""
        with self.transaction():
            deleted = self._write("DELETE FROM subscriptions WHERE id = ?",
                                  (subscription_id,)).rowcount > 0
            self._write("UPDATE outbox SET status = 'dead', last_error = 'unsubscribed' "
                        "WHERE subscription_id = ? AND status = 'pending'", (subscription_id,))
        return deleted

    def add_notification(self, subscription: Subscription, job_id: int,
                         payload: dict[str, Any], time: float) -> None:
        self._write("INSERT INTO outbox (subscription_id, url, job_id, payload, status, "
                    "attempts, next_attempt_at) VALUES (?, ?, ?, ?, 'pending', 0, ?)",
                    (subscription.id, subscription.url, job_id, json.dumps(payload), time))

    def list_notifications(self, status: str | None = None,
                           job_id: int | None = None) -> list[Notification]:
        sql, params = "SELECT * FROM outbox WHERE 1 = 1", []
        if status is not None:
            sql, params = sql + " AND status = ?", [*params, status]
        if job_id is not None:
            sql, params = sql + " AND job_id = ?", [*params, job_id]
        return [Notification(row["id"], row["subscription_id"], row["url"], row["job_id"],
                             json.loads(row["payload"]), row["status"], row["attempts"],
                             row["next_attempt_at"], row["last_error"])
                for row in self._rows(sql + " ORDER BY id", tuple(params))]

    def get_notification(self, notification_id: int) -> Notification | None:
        rows = self._rows("SELECT * FROM outbox WHERE id = ?", (notification_id,))
        return self._row_to_notification(rows[0]) if rows else None

    @staticmethod
    def _row_to_notification(row: sqlite3.Row) -> Notification:
        return Notification(row["id"], row["subscription_id"], row["url"], row["job_id"],
                            json.loads(row["payload"]), row["status"], row["attempts"],
                            row["next_attempt_at"], row["last_error"])

    def save_notification(self, note: Notification) -> None:
        self._write("UPDATE outbox SET status = ?, attempts = ?, next_attempt_at = ?, "
                    "last_error = ? WHERE id = ?",
                    (note.status, note.attempts, note.next_attempt_at, note.last_error, note.id))

    # ── audit log ──

    def add_audit(self, job_id: int, old_status: JobStatus | None, new_status: JobStatus,
                  time: float, reason: str) -> None:
        self._write("INSERT INTO audit_log (job_id, old_status, new_status, time, reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (job_id, old_status.value if old_status is not None else None,
                     new_status.value, time, reason))

    def audit_entries(self, job_id: int | None = None,
                      since: float | None = None) -> list[AuditEntry]:
        """Audit entries in the order they were written, for one job or after ``since``."""
        if job_id is not None:
            rows = self._rows("SELECT * FROM audit_log WHERE job_id = ? ORDER BY id", (job_id,))
        elif since is not None:
            rows = self._rows("SELECT * FROM audit_log WHERE time > ? ORDER BY id", (since,))
        else:
            rows = self._rows("SELECT * FROM audit_log ORDER BY id")
        return [AuditEntry(row["job_id"], row["old_status"], row["new_status"], row["time"],
                           row["reason"]) for row in rows]
