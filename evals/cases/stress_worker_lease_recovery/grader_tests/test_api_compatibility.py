from __future__ import annotations

import inspect

import pytest

from conftest import queue_request
from worker_queue import (
    IdempotencyConflict,
    InvalidStateTransition,
    JobNotFound,
    JobStatus,
    OperationConflict,
    QueueAPI,
    QueueApplication,
    QueueError,
    StaleLease,
    ValidationError,
    build_api,
    build_application,
)


def test_public_exports_types_and_method_signatures():
    assert issubclass(IdempotencyConflict, QueueError)
    assert issubclass(InvalidStateTransition, QueueError)
    assert issubclass(JobNotFound, QueueError)
    assert issubclass(OperationConflict, QueueError)
    assert issubclass(StaleLease, QueueError)
    assert issubclass(ValidationError, QueueError)
    assert JobStatus.LEASED.value == "leased"
    assert isinstance(build_application(), QueueApplication)
    assert isinstance(build_api(), QueueAPI)

    expected = {
        "submit": ["self", "request", "request_id", "now"],
        "claim": ["self", "worker_id", "now"],
        "complete": ["self", "job_id", "lease_token", "result", "now"],
        "fail": ["self", "job_id", "lease_token", "error", "retry_at", "now"],
        "cancel": ["self", "job_id", "now"],
        "recover": ["self", "now"],
        "get_job": ["self", "job_id"],
        "list_jobs": ["self"],
        "history": ["self", "job_id"],
    }
    for name, parameters in expected.items():
        assert list(inspect.signature(getattr(QueueAPI, name)).parameters) == parameters


def test_output_shapes_and_documented_errors(make_application):
    app = make_application()
    with pytest.raises(JobNotFound):
        app.api.get_job("job-999999")
    with pytest.raises(ValidationError):
        app.api.submit({"task": ""}, request_id="bad", now=0)

    job = app.api.submit(queue_request(), request_id="shape", now=0)
    assert set(job) == {
        "job_id", "request_id", "task", "payload", "max_attempts",
        "created_at", "status", "attempt", "lease_generation", "worker_id",
        "lease_token", "lease_expires_at", "retry_at", "result", "last_error",
    }
    claim = app.api.claim("worker", now=1)
    assert set(claim) == {
        "job_id", "task", "payload", "attempt", "lease_generation",
        "lease_token", "lease_expires_at",
    }
    assert app.api.claim("other", now=2) is None
    assert set(app.api.recover(now=2)) == {
        "expired_leases", "runnable_jobs", "held_leases", "terminal_jobs",
        "recovered_job_ids",
    }


def test_application_snapshot_is_detached(make_application):
    app = make_application()
    job = app.api.submit(
        queue_request(payload={"items": [1]}), request_id="snapshot-copy", now=0
    )
    snapshot = app.snapshot()
    snapshot["jobs"]["jobs"][0]["payload"]["items"].append(2)
    snapshot["events"]["events"][0]["details"]["changed"] = True

    assert app.api.get_job(job["job_id"])["payload"] == {"items": [1]}
    assert app.api.history()[0]["details"] == {}


def test_non_json_completion_result_is_rejected_without_mutation(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="bad-result", now=0)
    claim = app.api.claim("worker", now=1)
    before = app.snapshot()

    with pytest.raises(ValidationError):
        app.api.complete(job["job_id"], claim["lease_token"], {"bad": {1}}, now=2)
    assert app.snapshot() == before
