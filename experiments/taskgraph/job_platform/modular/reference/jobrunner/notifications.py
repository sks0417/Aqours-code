"""Webhook notifications through an outbox (SPEC.md "Webhook notifications").

A transitions subscriber writes one outbox row per matching subscription for
every state change; it runs inside the transaction of the change, so the
row is committed with it. ``deliver`` calls the injected sender for due rows,
in order per subscription and job, with exponential backoff and at most
``MAX_ATTEMPTS`` attempts. Registers the ``/subscriptions`` and
``/notifications`` routes and the ``/notifications`` page, where a dead
notification can be retried.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import transitions, web
from .errors import InvalidTransition, NotFound
from .models import Event, JobStatus, Notification, Subscription
from .store import JobStore, register_schema

if TYPE_CHECKING:
    from .runner import Runner

Sender = Callable[[str, dict[str, Any]], Any]

STATUSES = ("pending", "delivered", "dead")
MAX_ATTEMPTS = 5

register_schema("""CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, kinds TEXT, statuses TEXT)""")
register_schema("""CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id INTEGER NOT NULL,
    url TEXT NOT NULL, job_id INTEGER NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL, last_error TEXT)""")


def _subscription(row: sqlite3.Row) -> Subscription:
    return Subscription(row["id"], row["url"],
                        json.loads(row["kinds"]) if row["kinds"] is not None else None,
                        json.loads(row["statuses"]) if row["statuses"] is not None else None)


def _notification(row: sqlite3.Row) -> Notification:
    return Notification(row["id"], row["subscription_id"], row["url"], row["job_id"],
                        json.loads(row["payload"]), row["status"], row["attempts"],
                        row["next_attempt_at"], row["last_error"])


def subscribe(store: JobStore, url: Any, kinds: Any = None,
              statuses: Any = None) -> Subscription:
    """Store a subscription and return it."""
    if not isinstance(url, str) or not url:
        raise ValueError("url must be a non-empty string")
    if kinds is not None:
        if not isinstance(kinds, (list, tuple)) or not all(isinstance(k, str) for k in kinds):
            raise ValueError("kinds must be a list of strings")
        kinds = list(kinds)
    if statuses is not None:
        if not isinstance(statuses, (list, tuple)):
            raise ValueError("statuses must be a list of statuses")
        try:
            statuses = [JobStatus(status).value for status in statuses]
        except ValueError:
            raise ValueError(f"unknown status in {statuses!r}") from None
    cursor = store.execute(
        "INSERT INTO subscriptions (url, kinds, statuses) VALUES (?, ?, ?)",
        (url, None if kinds is None else json.dumps(kinds),
         None if statuses is None else json.dumps(statuses)))
    return Subscription(cursor.lastrowid, url, kinds, statuses)


def unsubscribe(store: JobStore, subscription_id: int) -> None:
    """Remove a subscription; its pending notifications become dead."""
    with store.transaction():
        if store.execute("DELETE FROM subscriptions WHERE id = ?",
                         (subscription_id,)).rowcount == 0:
            raise NotFound(f"subscription {subscription_id} not found")
        store.execute("UPDATE outbox SET status = 'dead', last_error = 'unsubscribed' "
                      "WHERE subscription_id = ? AND status = 'pending'", (subscription_id,))


def list_subscriptions(store: JobStore) -> list[Subscription]:
    """Every subscription, ordered by id."""
    return [_subscription(row) for row in store.query("SELECT * FROM subscriptions ORDER BY id")]


def list_notifications(store: JobStore, status: str | None = None,
                       job_id: int | None = None) -> list[Notification]:
    """Outbox rows ordered by id, optionally filtered."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown notification status {status!r}")
    sql, params = "SELECT * FROM outbox WHERE 1 = 1", []
    if status is not None:
        sql, params = sql + " AND status = ?", [*params, status]
    if job_id is not None:
        sql, params = sql + " AND job_id = ?", [*params, job_id]
    return [_notification(row) for row in store.query(sql + " ORDER BY id", tuple(params))]


def retry(store: JobStore, notification_id: int, *, now: float) -> Notification:
    """Make a dead notification pending again, with no attempts used."""
    rows = store.query("SELECT * FROM outbox WHERE id = ?", (notification_id,))
    if not rows:
        raise NotFound(f"notification {notification_id} not found")
    note = _notification(rows[0])
    if note.status != "dead":
        raise InvalidTransition(f"notification {notification_id} is {note.status}")
    if not store.query("SELECT id FROM subscriptions WHERE id = ?", (note.subscription_id,)):
        raise InvalidTransition(f"subscription {note.subscription_id} was removed")
    store.execute("UPDATE outbox SET status = 'pending', attempts = 0, next_attempt_at = ? "
                  "WHERE id = ?", (now, notification_id))
    return _notification(store.query("SELECT * FROM outbox WHERE id = ?",
                                     (notification_id,))[0])


def enqueue(store: JobStore, event: Event) -> None:
    """Subscriber: one outbox row per subscription that matches the change."""
    job = event.job
    payload = {"job_id": job.id, "kind": job.kind, "tenant": job.tenant,
               "old_status": event.old_status.value if event.old_status else None,
               "new_status": event.new_status.value, "reason": event.reason,
               "time": event.time}
    for sub in list_subscriptions(store):
        if sub.kinds is not None and job.kind not in sub.kinds:
            continue
        if sub.statuses is not None and event.new_status.value not in sub.statuses:
            continue
        store.execute(
            "INSERT INTO outbox (subscription_id, url, job_id, payload, status, attempts, "
            "next_attempt_at) VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (sub.id, sub.url, job.id, json.dumps({"subscription_id": sub.id, **payload}),
             event.time))


transitions.subscribe(enqueue)


def deliver(store: JobStore, sender: Sender, *, now: float) -> int:
    """Attempt every due pending notification in order; return how many were delivered."""
    blocked: set[tuple[int, int]] = set()
    delivered = 0
    for note in list_notifications(store, "pending"):
        group = (note.subscription_id, note.job_id)
        if group in blocked:
            continue
        if note.next_attempt_at > now:
            blocked.add(group)
            continue
        try:
            sender(note.url, note.payload)
        except Exception as exc:  # a BaseException is a crash and propagates
            note.attempts += 1
            note.last_error = str(exc)
            if note.attempts >= MAX_ATTEMPTS:
                note.status = "dead"
            else:
                note.next_attempt_at = now + 2 ** (note.attempts - 1)
                blocked.add(group)
        else:
            note.attempts += 1
            note.status = "delivered"
            delivered += 1
        store.execute("UPDATE outbox SET status = ?, attempts = ?, next_attempt_at = ?, "
                      "last_error = ? WHERE id = ?",
                      (note.status, note.attempts, note.next_attempt_at, note.last_error,
                       note.id))
    return delivered


@web.route("POST", r"/subscriptions")
def subscribe_route(runner: Runner, request: web.Request) -> web.Response:
    """``POST /subscriptions``: 201 and the subscription."""
    body = web.fields(request, "url")
    return 201, runner.subscribe(body["url"], body.get("kinds"), body.get("statuses")).to_dict()


@web.route("GET", r"/subscriptions")
def subscriptions_route(runner: Runner, request: web.Request) -> web.Response:
    """``GET /subscriptions``: 200 and every subscription."""
    return 200, {"subscriptions": [sub.to_dict() for sub in runner.list_subscriptions()]}


@web.route("DELETE", r"/subscriptions/(?P<subscription_id>\d+)")
def unsubscribe_route(runner: Runner, request: web.Request,
                      subscription_id: str) -> web.Response:
    """``DELETE /subscriptions/<id>``: 200 and ``{"deleted": id}``."""
    runner.unsubscribe(int(subscription_id))
    return 200, {"deleted": int(subscription_id)}


@web.route("GET", r"/notifications")
def notifications_route(runner: Runner, request: web.Request) -> web.Response:
    """``GET /notifications[?status=...&job_id=...]``: 200 and the outbox rows."""
    job_id = request.query.get("job_id")
    if job_id is not None and not job_id.isdigit():
        raise ValueError("job_id must be a number")
    notes = runner.list_notifications(request.query.get("status"),
                                      int(job_id) if job_id is not None else None)
    return 200, {"notifications": [note.to_dict() for note in notes]}


@web.route("POST", r"/notifications/(?P<notification_id>\d+)/retry")
def retry_route(runner: Runner, request: web.Request, notification_id: str) -> web.Response:
    """``POST /notifications/<id>/retry``: 200 and the notification."""
    return 200, runner.retry_notification(int(notification_id)).to_dict()


def _listed(values: list[str] | None) -> str:
    return "all" if values is None else ", ".join(values)


@web.page(r"/notifications")
def notifications_page(runner: Runner, request: web.Request) -> web.Page:
    """``/notifications``: subscriptions and the outbox."""
    subscribed = runner.list_subscriptions()
    live = {sub.id for sub in subscribed}

    def actions(note: Notification) -> web.Markup:
        if note.status != "dead" or note.subscription_id not in live:
            return web.Markup("")
        return web.Markup(f'<form method="post" action="/notifications/{note.id}/retry">'
                          '<button type="submit">Retry</button></form>')

    subscriptions = ([sub.id, sub.url, _listed(sub.kinds), _listed(sub.statuses)]
                     for sub in subscribed)
    outbox = ([note.id, note.subscription_id, note.job_id, note.payload["new_status"],
               note.status, note.attempts,
               web.time(note.next_attempt_at) if note.status == "pending" else "-",
               actions(note)]
              for note in runner.list_notifications())
    return 200, web.heading(
        "Notifications",
        web.table("subscriptions", ("ID", "URL", "Kinds", "Statuses"), subscriptions),
        web.table("outbox", ("ID", "Subscription", "Job", "Event", "Status", "Attempts",
                             "Next attempt", "Actions"), outbox))
