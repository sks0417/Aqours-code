"""Recurring job definitions (SPEC.md "Delayed and recurring jobs").

Definitions live in the ``recurring`` table. ``Runner.run_once`` calls
``create_due`` first: each active definition whose period has come creates a
job (unless its last job is unfinished) and moves to its next period after
now, so missed periods are skipped. Registers the ``/recurring`` routes and
the ``/recurring`` page.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import TYPE_CHECKING, Any

from . import transitions, web
from .errors import NotFound
from .models import Job, RecurringJob
from .store import JobStore, register_schema
from .validation import check_int, check_number, check_tenant

if TYPE_CHECKING:
    from .runner import Runner

register_schema("""CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL,
    interval_s REAL NOT NULL, next_run_at REAL NOT NULL, priority INTEGER NOT NULL,
    tenant TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0, last_job_id INTEGER)""")


def _definition(row: sqlite3.Row) -> RecurringJob:
    return RecurringJob(id=row["id"], kind=row["kind"], payload=json.loads(row["payload"]),
                        interval_s=row["interval_s"], next_run_at=row["next_run_at"],
                        priority=row["priority"], tenant=row["tenant"],
                        paused=bool(row["paused"]), last_job_id=row["last_job_id"])


def _check_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")


def schedule(store: JobStore, kind: str, payload: dict[str, Any], interval_s: float,
             start_at: float, *, priority: int = 0, tenant: str = "default") -> int:
    """Store a new definition whose first period is ``start_at``; return its id."""
    check_number("interval_s", interval_s, positive=True)
    check_number("start_at", start_at)
    check_int("priority", priority)
    check_tenant(tenant)
    _check_payload(payload)
    cursor = store.execute(
        "INSERT INTO recurring (kind, payload, interval_s, next_run_at, priority, tenant) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, json.dumps(payload), interval_s, start_at, priority, tenant))
    return cursor.lastrowid


def get(store: JobStore, definition_id: int) -> RecurringJob:
    """One definition; ``NotFound`` for an unknown id."""
    rows = store.query("SELECT * FROM recurring WHERE id = ?", (definition_id,))
    if not rows:
        raise NotFound(f"recurring definition {definition_id} not found")
    return _definition(rows[0])


def list_all(store: JobStore) -> list[RecurringJob]:
    """Every definition, ordered by id."""
    return [_definition(row) for row in store.query("SELECT * FROM recurring ORDER BY id")]


def update(store: JobStore, definition_id: int, *, payload: dict[str, Any] | None = None,
           interval_s: float | None = None, priority: int | None = None,
           tenant: str | None = None) -> RecurringJob:
    """Change the given fields of a definition; ``next_run_at`` stays."""
    definition = get(store, definition_id)
    if payload is not None:
        _check_payload(payload)
        definition.payload = dict(payload)
    if interval_s is not None:
        definition.interval_s = check_number("interval_s", interval_s, positive=True)
    if priority is not None:
        definition.priority = check_int("priority", priority)
    if tenant is not None:
        definition.tenant = check_tenant(tenant)
    store.execute("UPDATE recurring SET payload = ?, interval_s = ?, priority = ?, tenant = ? "
                  "WHERE id = ?", (json.dumps(definition.payload), definition.interval_s,
                                   definition.priority, definition.tenant, definition_id))
    return get(store, definition_id)


def set_paused(store: JobStore, definition_id: int, paused: bool) -> RecurringJob:
    """Pause or resume a definition and return it."""
    get(store, definition_id)
    store.execute("UPDATE recurring SET paused = ? WHERE id = ?", (int(paused), definition_id))
    return get(store, definition_id)


def delete(store: JobStore, definition_id: int) -> None:
    """Remove a definition; the jobs it created keep their ``definition_id``."""
    get(store, definition_id)
    store.execute("DELETE FROM recurring WHERE id = ?", (definition_id,))


def jobs_of(store: JobStore, definition_id: int) -> list[Job]:
    """The jobs a definition created, ordered by id."""
    get(store, definition_id)
    return [job for job in store.list() if job.definition_id == definition_id]


def next_period(definition: RecurringJob, now: float) -> float:
    """The first period time of ``definition`` that is later than ``now``."""
    periods = math.floor((now - definition.next_run_at) / definition.interval_s) + 1
    following = definition.next_run_at + max(periods, 1) * definition.interval_s
    while following <= now:
        following += definition.interval_s
    return following


def create_due(store: JobStore, now: float, *, max_attempts: int) -> None:
    """Create the jobs of every active definition whose period has come."""
    for definition in list_all(store):
        if definition.paused or definition.next_run_at > now:
            continue
        last_job_id = definition.last_job_id
        last = store.get(last_job_id) if last_job_id is not None else None
        if last is None or last.status.is_final:
            job = transitions.create(store, definition.kind, dict(definition.payload), now=now,
                                     max_attempts=max_attempts, priority=definition.priority,
                                     tenant=definition.tenant, definition_id=definition.id)
            last_job_id = job.id
        store.execute("UPDATE recurring SET next_run_at = ?, last_job_id = ? WHERE id = ?",
                      (next_period(definition, now), last_job_id, definition.id))


@web.route("POST", r"/recurring")
def create_route(runner: Runner, request: web.Request) -> web.Response:
    """``POST /recurring``: 201 and the new definition."""
    body = web.fields(request, "kind", "interval_s")
    payload = body.get("payload", {})
    options = {name: body[name] for name in ("start_at", "priority", "tenant") if name in body}
    definition_id = runner.schedule_recurring(body["kind"], payload, body["interval_s"],
                                              **options)
    return 201, runner.get_recurring(definition_id).to_dict()


@web.route("GET", r"/recurring")
def list_route(runner: Runner, request: web.Request) -> web.Response:
    """``GET /recurring``: 200 and every definition."""
    return 200, {"recurring": [d.to_dict() for d in runner.list_recurring()]}


@web.route("GET", r"/recurring/(?P<definition_id>\d+)")
def get_route(runner: Runner, request: web.Request, definition_id: str) -> web.Response:
    """``GET /recurring/<id>``: 200 and the definition."""
    return 200, runner.get_recurring(int(definition_id)).to_dict()


@web.route("PATCH", r"/recurring/(?P<definition_id>\d+)")
def update_route(runner: Runner, request: web.Request, definition_id: str) -> web.Response:
    """``PATCH /recurring/<id>``: 200 and the changed definition."""
    changes = {name: value for name, value in web.fields(request).items()
               if name in ("payload", "interval_s", "priority", "tenant")}
    return 200, runner.update_recurring(int(definition_id), **changes).to_dict()


@web.route("GET", r"/recurring/(?P<definition_id>\d+)/jobs")
def jobs_route(runner: Runner, request: web.Request, definition_id: str) -> web.Response:
    """``GET /recurring/<id>/jobs``: 200 and the jobs the definition created."""
    jobs = runner.recurring_jobs(int(definition_id))
    return 200, {"definition_id": int(definition_id), "jobs": [job.to_dict() for job in jobs]}


@web.route("POST", r"/recurring/(?P<definition_id>\d+)/pause")
def pause_route(runner: Runner, request: web.Request, definition_id: str) -> web.Response:
    """``POST /recurring/<id>/pause``: 200 and the definition."""
    return 200, runner.pause_recurring(int(definition_id)).to_dict()


@web.route("POST", r"/recurring/(?P<definition_id>\d+)/resume")
def resume_route(runner: Runner, request: web.Request, definition_id: str) -> web.Response:
    """``POST /recurring/<id>/resume``: 200 and the definition."""
    return 200, runner.resume_recurring(int(definition_id)).to_dict()


@web.route("DELETE", r"/recurring/(?P<definition_id>\d+)")
def delete_route(runner: Runner, request: web.Request, definition_id: str) -> web.Response:
    """``DELETE /recurring/<id>``: 200 and ``{"deleted": id}``."""
    runner.delete_recurring(int(definition_id))
    return 200, {"deleted": int(definition_id)}


def _actions(definition: RecurringJob) -> web.Markup:
    action, label = ("resume", "Resume") if definition.paused else ("pause", "Pause")
    return web.Markup(f'<form method="post" action="/recurring/{definition.id}/{action}">'
                      f'<button type="submit">{label}</button></form>')


@web.page(r"/recurring")
def recurring_page(runner: Runner, request: web.Request) -> web.Page:
    """``/recurring``: every definition with a pause or resume form."""
    rows = ([d.id, d.kind, web.time(d.interval_s), web.time(d.next_run_at),
             "paused" if d.paused else "active",
             d.last_job_id if d.last_job_id is not None else "-", _actions(d)]
            for d in runner.list_recurring())
    columns = ("ID", "Kind", "Interval", "Next run", "State", "Last job", "Actions")
    return 200, web.heading("Recurring jobs", web.table("recurring", columns, rows))
