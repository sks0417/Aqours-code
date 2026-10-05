"""Errors raised by the job runner."""
from __future__ import annotations


class JobRunnerError(Exception):
    """Base class for job runner errors."""


class NotFound(JobRunnerError):
    """No record has the requested id or name."""


class JobNotFound(NotFound):
    """No job has the requested id."""

    def __init__(self, job_id: int):
        super().__init__(f"job {job_id} not found")
        self.job_id = job_id


class InvalidTransition(JobRunnerError):
    """The job's current state does not allow the requested change."""


class TransientError(Exception):
    """Raised by a handler for a failure worth retrying."""
