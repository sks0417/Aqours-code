"""Errors raised by the job runner."""
from __future__ import annotations


class JobRunnerError(Exception):
    """Base class for job runner errors."""


class JobNotFound(JobRunnerError):
    """No job has the requested id."""

    def __init__(self, job_id: int):
        super().__init__(f"job {job_id} not found")
        self.job_id = job_id
