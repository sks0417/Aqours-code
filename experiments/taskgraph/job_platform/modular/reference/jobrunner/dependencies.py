"""Dependencies between jobs (SPEC.md "Dependencies").

A scheduler filter lets a job run only when every job in its ``depends_on``
has succeeded. A transitions subscriber cancels the pending dependents of a
job that failed or was cancelled (and so on downstream, as each cancellation
is itself an event), and cancels a new job whose dependency had already
failed. Registers ``GET /jobs/<id>/dependencies`` and its page.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import scheduler, transitions, web
from .errors import JobNotFound
from .models import Event, Job, JobStatus
from .store import JobStore
from .validation import is_int

if TYPE_CHECKING:
    from .runner import Runner

DEPENDENCY_FAILED = "dependency_failed"
BROKEN = (JobStatus.FAILED, JobStatus.CANCELLED)


def check_new(store: JobStore, depends_on: Any) -> list[int]:
    """Validate the ``depends_on`` of a new job; return it without repeats."""
    if not isinstance(depends_on, (list, tuple)):
        raise ValueError("depends_on must be a list of job ids")
    ids: list[int] = []
    for job_id in depends_on:
        if not is_int(job_id):
            raise ValueError("depends_on must be a list of job ids")
        if store.get(job_id) is None:
            raise ValueError(f"dependency {job_id} does not exist")
        if job_id not in ids:
            ids.append(job_id)
    return ids


def dependencies_succeeded(store: JobStore, job: Job, now: float) -> bool:
    """Filter: every dependency of ``job`` has SUCCEEDED."""
    for dependency_id in job.depends_on:
        dependency = store.get(dependency_id)
        if dependency is None or dependency.status is not JobStatus.SUCCEEDED:
            return False
    return True


scheduler.register_filter("dependencies", dependencies_succeeded)


def dependents(store: JobStore, job_id: int) -> list[Job]:
    """Jobs that list ``job_id`` in ``depends_on``, ordered by id."""
    if store.get(job_id) is None:
        raise JobNotFound(job_id)
    return [job for job in store.list() if job_id in job.depends_on]


def cascade(store: JobStore, event: Event) -> None:
    """Subscriber: cancel pending jobs whose dependency failed or was cancelled."""
    if event.new_status in BROKEN:
        for job in store.list(JobStatus.PENDING):
            if event.job.id in job.depends_on:
                transitions.change(store, job.id, JobStatus.CANCELLED,
                                   reason=DEPENDENCY_FAILED, now=event.time)
    elif event.old_status is None:
        broken = [dep for dep in map(store.get, event.job.depends_on)
                  if dep is not None and dep.status in BROKEN]
        if broken:
            transitions.change(store, event.job.id, JobStatus.CANCELLED,
                               reason=DEPENDENCY_FAILED, now=event.time)


transitions.subscribe(cascade)


@web.route("GET", r"/jobs/(?P<job_id>\d+)/dependencies")
def dependencies_route(runner: Runner, request: web.Request, job_id: str) -> web.Response:
    """``GET /jobs/<id>/dependencies``: the jobs it waits for and the jobs waiting for it."""
    job = runner.get(int(job_id))
    return 200, {"job_id": job.id,
                 "depends_on": [runner.get(dep).to_dict() for dep in job.depends_on],
                 "dependents": [dep.to_dict() for dep in runner.dependents(job.id)]}


@web.page(r"/jobs/(?P<job_id>\d+)/dependencies")
def dependencies_page(runner: Runner, request: web.Request, job_id: str) -> web.Page:
    """``/jobs/<id>/dependencies``."""
    job = runner.get(int(job_id))
    rows = [["depends on", dep.id, dep.kind, dep.status.value]
            for dep in map(runner.get, job.depends_on)]
    rows += [["dependent", dep.id, dep.kind, dep.status.value]
             for dep in runner.dependents(job.id)]
    return 200, web.heading(f"Job {job.id} dependencies", web.table(
        "dependencies", ("Relation", "ID", "Kind", "Status"), rows))
