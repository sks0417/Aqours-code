"""A framework-free REST API: ``handle(runner, method, path, body)``.

Each module registers its own routes with ``jobrunner.web.route``; this
module registers the core job routes. Importing ``jobrunner.runner``
imports every module that has routes.
"""
from __future__ import annotations

from typing import Any

from . import web
from .models import JobStatus
from .runner import Runner  # importing it loads every module's routes


@web.route("POST", r"/jobs")
def create_job(runner: Runner, request: web.Request) -> web.Response:
    """``POST /jobs``: 201 and the new job."""
    body = request.body
    if not isinstance(body, dict) or not isinstance(body.get("kind"), str):
        return web.error(400, "body must be an object with a string 'kind'")
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        return web.error(400, "'payload' must be an object")
    options = {name: body[name] for name in ("priority", "tenant", "run_at", "depends_on")
               if name in body}
    return 201, runner.submit(body["kind"], payload, **options).to_dict()


@web.route("GET", r"/jobs")
def list_jobs(runner: Runner, request: web.Request) -> web.Response:
    """``GET /jobs[?status=...]``: 200 and the jobs ordered by id."""
    status = None
    if "status" in request.query:
        try:
            status = JobStatus(request.query["status"])
        except ValueError:
            return web.error(400, f"unknown status {request.query['status']!r}")
    return 200, {"jobs": [job.to_dict() for job in runner.list(status)]}


@web.route("GET", r"/jobs/(?P<job_id>\d+)")
def get_job(runner: Runner, request: web.Request, job_id: str) -> web.Response:
    """``GET /jobs/<id>``: 200 and the job."""
    return 200, runner.get(int(job_id)).to_dict()


def handle(runner: Runner, method: str, path: str, body: Any = None) -> web.Response:
    """Dispatch one request and return ``(status_code, json_body)``."""
    return web.dispatch(runner, method, path, body)

