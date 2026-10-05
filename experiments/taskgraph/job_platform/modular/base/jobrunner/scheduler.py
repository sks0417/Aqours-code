"""Choosing the next job to run, from composable rules.

A *filter* ``rule(store, job, now) -> bool`` says whether a pending job may
run now; a job can run only if every registered filter accepts it. An
*ordering* ``rule(store, job) -> key`` ranks the jobs that can run:
``pick_next`` sorts by the keys of all orderings, in ascending ``rank``, and
breaks ties by ``next_run_at`` and then id. Modules register their rules when
they are imported.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import Job, JobStatus
from .store import JobStore

Filter = Callable[[JobStore, Job, float], bool]
Ordering = Callable[[JobStore, Job], Any]

_filters: dict[str, Filter] = {}
_orderings: dict[str, tuple[int, Ordering]] = {}


def register_filter(name: str, rule: Filter) -> None:
    """Add (or replace) the filter called ``name``."""
    _filters[name] = rule


def register_ordering(name: str, rank: int, rule: Ordering) -> None:
    """Add (or replace) the ordering called ``name``; lower ranks sort first."""
    _orderings[name] = (rank, rule)


def is_due(store: JobStore, job: Job, now: float) -> bool:
    """A job may not run before its ``next_run_at``."""
    return job.next_run_at <= now


register_filter("due", is_due)


def can_run(store: JobStore, now: float) -> list[Job]:
    """Pending jobs that every filter accepts, ordered by id."""
    return [job for job in store.list(JobStatus.PENDING)
            if all(rule(store, job, now) for rule in _filters.values())]


def sort_key(store: JobStore, job: Job) -> tuple[Any, ...]:
    """The key that ``pick_next`` minimizes."""
    rules = [rule for _, rule in sorted(_orderings.values(), key=lambda item: item[0])]
    return (*(rule(store, job) for rule in rules), job.next_run_at, job.id)


def pick_next(store: JobStore, now: float) -> Job | None:
    """Return the job to run next at time ``now``, or None."""
    return min(can_run(store, now), key=lambda job: sort_key(store, job), default=None)
