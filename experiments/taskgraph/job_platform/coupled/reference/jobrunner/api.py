"""A framework-free REST API: ``handle(runner, method, path, body)``."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import InvalidTransition, NotFound
from .models import JobStatus
from .runner import Runner

Response = tuple[int, dict[str, Any]]

_JOB_PATH = re.compile(r"^/jobs/(\d+)$")
_CANCEL_PATH = re.compile(r"^/jobs/(\d+)/cancel$")
_PRIORITY_PATH = re.compile(r"^/jobs/(\d+)/priority$")
_DEPENDENCIES_PATH = re.compile(r"^/jobs/(\d+)/dependencies$")
_HISTORY_PATH = re.compile(r"^/jobs/(\d+)/history$")
_RECURRING_PATH = re.compile(r"^/recurring/(\d+)$")
_RECURRING_ACTION_PATH = re.compile(r"^/recurring/(\d+)/(pause|resume)$")
_RATE_LIMIT_PATH = re.compile(r"^/rate-limits/([^/]+)$")
_SUBSCRIPTION_PATH = re.compile(r"^/subscriptions/(\d+)$")
_RECURRING_JOBS_PATH = re.compile(r"^/recurring/(\d+)/jobs$")
_RETRY_PATH = re.compile(r"^/notifications/(\d+)/retry$")

_JOB_OPTIONS = ("priority", "tenant", "run_at", "depends_on")
_RECURRING_OPTIONS = ("start_at", "priority", "tenant")


def _error(status: int, message: str) -> Response:
    return status, {"error": message}


def _object(body: Any, *required: str) -> dict[str, Any]:
    """The request body as a dict that has every ``required`` field."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    missing = [name for name in required if name not in body]
    if missing:
        raise ValueError(f"missing field(s): {', '.join(missing)}")
    return body


def _number(text: str, name: str) -> float:
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{name} must be a number") from None


def _create(runner: Runner, body: Any) -> Response:
    if not isinstance(body, dict) or not isinstance(body.get("kind"), str):
        return _error(400, "body must be an object with a string 'kind'")
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        return _error(400, "'payload' must be an object")
    options = {name: body[name] for name in _JOB_OPTIONS if name in body}
    job = runner.submit(body["kind"], payload, **options)
    return 201, job.to_dict()


def _list(runner: Runner, query: dict[str, list[str]]) -> Response:
    values = query.get("status")
    status = None
    if values:
        try:
            status = JobStatus(values[0])
        except ValueError:
            return _error(400, f"unknown status {values[0]!r}")
    return 200, {"jobs": [job.to_dict() for job in runner.list(status)]}


def _create_recurring(runner: Runner, body: Any) -> Response:
    body = _object(body, "kind", "interval_s")
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("'payload' must be an object")
    options = {name: body[name] for name in _RECURRING_OPTIONS if name in body}
    definition_id = runner.schedule_recurring(body["kind"], payload, body["interval_s"],
                                              **options)
    return 201, runner.get_recurring(definition_id).to_dict()


def _notifications(runner: Runner, query: dict[str, list[str]]) -> Response:
    status = query.get("status", [None])[0]
    job_id = query.get("job_id", [None])[0]
    if job_id is not None:
        if not job_id.isdigit():
            raise ValueError("job_id must be a number")
        job_id = int(job_id)
    notes = runner.list_notifications(status=status, job_id=job_id)
    return 200, {"notifications": [note.to_dict() for note in notes]}


def _stats(runner: Runner, query: dict[str, list[str]]) -> Response:
    window = query.get("window_s", [None])[0]
    window_s = None if window is None else _number(window, "window_s")
    return 200, runner.stats(window_s)


def _dependencies(runner: Runner, job_id: int) -> Response:
    job = runner.get(job_id)
    return 200, {"job_id": job_id,
                 "depends_on": [runner.get(dep).to_dict() for dep in job.depends_on],
                 "dependents": [dep.to_dict() for dep in runner.dependents(job_id)]}


def _route(runner: Runner, method: str, path: str, query: dict[str, list[str]],
           body: Any) -> Response:
    if path == "/jobs":
        if method == "POST":
            return _create(runner, body)
        if method == "GET":
            return _list(runner, query)
    match = _JOB_PATH.match(path)
    if match and method == "GET":
        return 200, runner.get(int(match.group(1))).to_dict()
    match = _CANCEL_PATH.match(path)
    if match and method == "POST":
        return 200, runner.cancel(int(match.group(1))).to_dict()
    match = _PRIORITY_PATH.match(path)
    if match and method == "POST":
        priority = _object(body, "priority")["priority"]
        return 200, runner.set_priority(int(match.group(1)), priority).to_dict()
    if path == "/queue" and method == "GET":
        return 200, {"jobs": [job.to_dict() for job in runner.queue()]}
    if path == "/tenants" and method == "GET":
        return 200, {"tenants": runner.tenants()}
    match = _DEPENDENCIES_PATH.match(path)
    if match and method == "GET":
        return _dependencies(runner, int(match.group(1)))
    match = _HISTORY_PATH.match(path)
    if match and method == "GET":
        job_id = int(match.group(1))
        return 200, {"job_id": job_id,
                     "history": [entry.to_dict() for entry in runner.history(job_id)]}
    if path == "/stats" and method == "GET":
        return _stats(runner, query)
    if path == "/audit" and method == "GET":
        since = query.get("since", [None])[0]
        entries = runner.audit_log(None if since is None else _number(since, "since"))
        return 200, {"entries": [entry.to_dict() for entry in entries]}
    if path == "/recurring":
        if method == "POST":
            return _create_recurring(runner, body)
        if method == "GET":
            return 200, {"recurring": [d.to_dict() for d in runner.list_recurring()]}
    match = _RECURRING_PATH.match(path)
    if match and method == "GET":
        return 200, runner.get_recurring(int(match.group(1))).to_dict()
    if match and method == "PATCH":
        changes = {name: value for name, value in _object(body).items()
                   if name in ("payload", "interval_s", "priority", "tenant")}
        return 200, runner.update_recurring(int(match.group(1)), **changes).to_dict()
    if match and method == "DELETE":
        runner.delete_recurring(int(match.group(1)))
        return 200, {"deleted": int(match.group(1))}
    match = _RECURRING_JOBS_PATH.match(path)
    if match and method == "GET":
        definition_id = int(match.group(1))
        jobs = runner.recurring_jobs(definition_id)
        return 200, {"definition_id": definition_id, "jobs": [job.to_dict() for job in jobs]}
    match = _RECURRING_ACTION_PATH.match(path)
    if match and method == "POST":
        definition_id, action = int(match.group(1)), match.group(2)
        if action == "pause":
            return 200, runner.pause_recurring(definition_id).to_dict()
        return 200, runner.resume_recurring(definition_id).to_dict()
    if path == "/rate-limits" and method == "GET":
        return 200, {"rate_limits": [limit.to_dict() for limit in runner.list_rate_limits()]}
    match = _RATE_LIMIT_PATH.match(path)
    if match and method == "PUT":
        body = _object(body, "max_starts", "window_s")
        kind = unquote(match.group(1))
        return 200, runner.set_rate_limit(kind, body["max_starts"], body["window_s"]).to_dict()
    if match and method == "DELETE":
        kind = unquote(match.group(1))
        runner.clear_rate_limit(kind)
        return 200, {"deleted": kind}
    if path == "/subscriptions":
        if method == "POST":
            body = _object(body, "url")
            subscription = runner.subscribe(body["url"], body.get("kinds"),
                                            body.get("statuses"))
            return 201, subscription.to_dict()
        if method == "GET":
            return 200, {"subscriptions": [s.to_dict() for s in runner.list_subscriptions()]}
    match = _SUBSCRIPTION_PATH.match(path)
    if match and method == "DELETE":
        runner.unsubscribe(int(match.group(1)))
        return 200, {"deleted": int(match.group(1))}
    if path == "/notifications" and method == "GET":
        return _notifications(runner, query)
    match = _RETRY_PATH.match(path)
    if match and method == "POST":
        return 200, runner.retry_notification(int(match.group(1))).to_dict()
    return _error(404, f"no route for {method} {path}")


def handle(runner: Runner, method: str, path: str, body: Any = None) -> Response:
    """Dispatch one request and return ``(status_code, json_body)``."""
    parts = urlsplit(path)
    query = parse_qs(parts.query, keep_blank_values=True)
    try:
        return _route(runner, method.upper(), parts.path, query, body)
    except ValueError as exc:
        return _error(400, str(exc))
    except NotFound as exc:
        return _error(404, str(exc))
    except InvalidTransition as exc:
        return _error(409, str(exc))
