from __future__ import annotations

import pytest

from conftest import event_types, queue_request
from worker_queue import OperationConflict, StaleLease, build_application


def test_failure_does_not_double_count_claim_attempt(make_application):
    app = make_application()
    job = app.api.submit(
        queue_request(max_attempts=3), request_id="attempt-count", now=0
    )
    claim = app.api.claim("worker", now=1)
    failed = app.api.fail(
        job["job_id"], claim["lease_token"], "temporary", retry_at=8, now=2
    )

    assert failed["attempt"] == 1
    assert failed["status"] == "retry_waiting"
    assert app.api.claim("early", now=7.999) is None
    assert app.api.claim("ready", now=8)["attempt"] == 2


def test_exact_failure_retry_survives_restart_without_new_history(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="failure-receipt", now=0)
    claim = app.api.claim("worker", now=1)
    first = app.api.fail(
        job["job_id"], claim["lease_token"], "busy", retry_at=20, now=2
    )
    restarted = build_application(app.snapshot(), lease_seconds=10)
    before = restarted.snapshot()
    repeated = restarted.api.fail(
        job["job_id"], claim["lease_token"], "busy", retry_at=20.0, now=9
    )

    assert repeated == first
    assert restarted.snapshot() == before
    assert event_types(restarted.api, job["job_id"]).count("retry_scheduled") == 1


def test_changed_duplicate_failure_conflicts_without_side_effects(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="failure-conflict", now=0)
    claim = app.api.claim("worker", now=1)
    app.api.fail(job["job_id"], claim["lease_token"], "busy", retry_at=20, now=2)
    before = app.snapshot()

    with pytest.raises(OperationConflict):
        app.api.fail(
            job["job_id"], claim["lease_token"], "different", retry_at=20, now=3
        )
    with pytest.raises(OperationConflict):
        app.api.fail(
            job["job_id"], claim["lease_token"], "busy", retry_at=21, now=3
        )
    assert app.snapshot() == before


def test_max_attempts_becomes_terminal_only_after_last_claim(make_application):
    app = make_application()
    job = app.api.submit(
        queue_request(max_attempts=2), request_id="two-attempts", now=0
    )
    first = app.api.claim("worker-a", now=1)
    retrying = app.api.fail(
        job["job_id"], first["lease_token"], "one", retry_at=3, now=2
    )
    second = app.api.claim("worker-b", now=3)
    terminal = app.api.fail(
        job["job_id"], second["lease_token"], "two", retry_at=5, now=4
    )

    assert retrying["status"] == "retry_waiting"
    assert retrying["attempt"] == 1
    assert terminal["status"] == "failed"
    assert terminal["attempt"] == 2
    assert terminal["retry_at"] is None
    assert app.api.claim("worker-c", now=100) is None
    assert event_types(app.api, job["job_id"])[-2:] == ["claimed", "failed"]


def test_different_operation_with_consumed_token_is_stale(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="cross-operation", now=0)
    claim = app.api.claim("worker", now=1)
    app.api.fail(job["job_id"], claim["lease_token"], "busy", retry_at=10, now=2)
    before = app.snapshot()

    with pytest.raises(StaleLease):
        app.api.complete(job["job_id"], claim["lease_token"], "wrong", now=3)
    assert app.snapshot() == before
