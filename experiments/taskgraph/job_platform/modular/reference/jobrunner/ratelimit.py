"""Per-kind rate limits (SPEC.md "Rate limits").

A transitions subscriber records every start in ``job_starts``; a scheduler
filter skips a job whose kind already started ``max_starts`` times within
the last ``window_s`` seconds. Limits live in ``rate_limits``, so both
survive a restart. Registers the ``/rate-limits`` routes and page.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from . import scheduler, transitions, web
from .errors import NotFound
from .models import Event, Job, JobStatus, RateLimit
from .store import JobStore, register_schema
from .validation import check_int, check_number

if TYPE_CHECKING:
    from .runner import Runner

register_schema("""CREATE TABLE IF NOT EXISTS rate_limits (
    kind TEXT PRIMARY KEY, max_starts INTEGER NOT NULL, window_s REAL NOT NULL)""")
register_schema("""CREATE TABLE IF NOT EXISTS job_starts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, time REAL NOT NULL)""")


def recent_starts(store: JobStore, kind: str, now: float, window_s: float) -> int:
    """Starts of ``kind`` with ``now - window_s < time <= now``."""
    rows = store.query("SELECT COUNT(*) AS n FROM job_starts "
                       "WHERE kind = ? AND time > ? AND time <= ?", (kind, now - window_s, now))
    return rows[0]["n"]


def record_start(store: JobStore, event: Event) -> None:
    """Subscriber: remember every start of every kind."""
    if event.new_status is JobStatus.RUNNING:
        store.execute("INSERT INTO job_starts (kind, time) VALUES (?, ?)",
                      (event.job.kind, event.time))


transitions.subscribe(record_start)


def list_limits(store: JobStore, *, now: float) -> list[RateLimit]:
    """Every limit, ordered by kind, with its ``recent_starts`` at ``now``."""
    rows = store.query("SELECT * FROM rate_limits ORDER BY kind")
    return [RateLimit(row["kind"], row["max_starts"], row["window_s"],
                      recent_starts(store, row["kind"], now, row["window_s"]))
            for row in rows]


def under_limit(store: JobStore, job: Job, now: float) -> bool:
    """Filter: the job's kind has not used up its limit."""
    rows = store.query("SELECT * FROM rate_limits WHERE kind = ?", (job.kind,))
    if not rows:
        return True
    return recent_starts(store, job.kind, now, rows[0]["window_s"]) < rows[0]["max_starts"]


scheduler.register_filter("rate_limit", under_limit)


def set_limit(store: JobStore, kind: str, max_starts: int, window_s: float, *,
              now: float) -> RateLimit:
    """Set (or replace) the limit of ``kind`` and return it."""
    check_int("max_starts", max_starts, minimum=1)
    check_number("window_s", window_s, positive=True)
    store.execute("INSERT OR REPLACE INTO rate_limits (kind, max_starts, window_s) "
                  "VALUES (?, ?, ?)", (kind, max_starts, window_s))
    return RateLimit(kind, max_starts, window_s, recent_starts(store, kind, now, window_s))


def clear_limit(store: JobStore, kind: str) -> None:
    """Remove the limit of ``kind``; ``NotFound`` if there is none."""
    if store.execute("DELETE FROM rate_limits WHERE kind = ?", (kind,)).rowcount == 0:
        raise NotFound(f"no rate limit for {kind!r}")


@web.route("GET", r"/rate-limits")
def list_route(runner: Runner, request: web.Request) -> web.Response:
    """``GET /rate-limits``: 200 and every limit."""
    return 200, {"rate_limits": [limit.to_dict() for limit in runner.list_rate_limits()]}


@web.route("PUT", r"/rate-limits/(?P<kind>[^/]+)")
def set_route(runner: Runner, request: web.Request, kind: str) -> web.Response:
    """``PUT /rate-limits/<kind>`` with ``{"max_starts", "window_s"}``: 200 and the limit."""
    body = web.fields(request, "max_starts", "window_s")
    limit = runner.set_rate_limit(unquote(kind), body["max_starts"], body["window_s"])
    return 200, limit.to_dict()


@web.route("DELETE", r"/rate-limits/(?P<kind>[^/]+)")
def clear_route(runner: Runner, request: web.Request, kind: str) -> web.Response:
    """``DELETE /rate-limits/<kind>``: 200 and ``{"deleted": kind}``."""
    runner.clear_rate_limit(unquote(kind))
    return 200, {"deleted": unquote(kind)}


@web.page(r"/rate-limits")
def rate_limits_page(runner: Runner, request: web.Request) -> web.Page:
    """``/rate-limits``: every limit and how much of it is used now."""
    rows = ([limit.kind, limit.max_starts, web.time(limit.window_s), limit.recent_starts]
            for limit in runner.list_rate_limits())
    columns = ("Kind", "Max starts", "Window", "Recent starts")
    return 200, web.heading("Rate limits", web.table("rate-limits", columns, rows))
