"""A framework-free REST API: ``handle(runner, method, path, body)``."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import InvalidTransition, JobNotFound
from .models import JobStatus
from .runner import Runner

Response = tuple[int, dict[str, Any]]

_JOB_PATH = re.compile(r"^/jobs/(\d+)$")
_CANCEL_PATH = re.compile(r"^/jobs/(\d+)/cancel$")


def _error(status: int, message: str) -> Response:
    return status, {"error": message}


def _create(runner: Runner, body: Any) -> Response:
    if not isinstance(body, dict) or not isinstance(body.get("kind"), str):
        return _error(400, "body must be an object with a string 'kind'")
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        return _error(400, "'payload' must be an object")
    try:
        job = runner.submit(body["kind"], payload)
    except ValueError as exc:
        return _error(400, str(exc))
    return 201, job.to_dict()


def _list(runner: Runner, query: str) -> Response:
    values = parse_qs(query).get("status")
    status = None
    if values:
        try:
            status = JobStatus(values[0])
        except ValueError:
            return _error(400, f"unknown status {values[0]!r}")
    return 200, {"jobs": [job.to_dict() for job in runner.list(status)]}


def _cancel(runner: Runner, job_id: int) -> Response:
    try:
        return 200, runner.cancel(job_id).to_dict()
    except JobNotFound as exc:
        return _error(404, str(exc))
    except InvalidTransition as exc:
        return _error(409, str(exc))


def handle(runner: Runner, method: str, path: str, body: Any = None) -> Response:
    """Dispatch one request and return ``(status_code, json_body)``."""
    parts = urlsplit(path)
    method = method.upper()
    if parts.path == "/jobs":
        if method == "POST":
            return _create(runner, body)
        if method == "GET":
            return _list(runner, parts.query)
    match = _JOB_PATH.match(parts.path)
    if match and method == "GET":
        try:
            return 200, runner.get(int(match.group(1))).to_dict()
        except JobNotFound as exc:
            return _error(404, str(exc))
    match = _CANCEL_PATH.match(parts.path)
    if match and method == "POST":
        return _cancel(runner, int(match.group(1)))
    return _error(404, f"no route for {method} {parts.path}")
