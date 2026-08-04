from __future__ import annotations

import pytest

from conftest import event_types, queue_request
from worker_queue import OperationConflict, StaleLease


def test_old_completion_cannot_commit_during_new_generation(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="stale-complete", now=0)
    old = app.api.claim("worker-a", now=1)
    new = app.api.claim("worker-b", now=11)
    before = app.snapshot()

    with pytest.raises(StaleLease):
        app.api.complete(job["job_id"], old["lease_token"], "late", now=12)

    assert app.snapshot() == before
    completed = app.api.complete(job["job_id"], new["lease_token"], "current", now=12)
    assert completed["result"] == "current"


def test_old_failure_cannot_reschedule_new_generation(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="stale-fail", now=0)
    old = app.api.claim("worker-a", now=1)
    current = app.api.claim("worker-b", now=11)
    before = app.snapshot()

    with pytest.raises(StaleLease):
        app.api.fail(
            job["job_id"], old["lease_token"], "old error", retry_at=30, now=12
        )

    assert app.snapshot() == before
    app.api.complete(job["job_id"], current["lease_token"], "ok", now=12)


def test_forged_prefix_token_and_expired_current_token_are_rejected(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="forgery", now=0)
    claim = app.api.claim("worker", now=1)

    for token, now in [
        (f"{job['job_id']}:invented", 2),
        (claim["lease_token"], claim["lease_expires_at"]),
    ]:
        before = app.snapshot()
        with pytest.raises(StaleLease):
            app.api.complete(job["job_id"], token, "no", now=now)
        assert app.snapshot() == before


def test_expiry_boundary_releases_once_and_preserves_submission_order(make_application):
    app = make_application()
    first = app.api.submit(queue_request("first"), request_id="first", now=0)
    second = app.api.submit(queue_request("second"), request_id="second", now=0)
    old = app.api.claim("worker-a", now=5)

    assert app.api.claim("blocked", now=14.999) ["job_id"] == second["job_id"]
    replacement = app.api.claim("replacement", now=15)
    assert replacement["job_id"] == first["job_id"]
    assert replacement["attempt"] == 2
    assert replacement["lease_generation"] == old["lease_generation"] + 1
    assert event_types(app.api, first["job_id"]).count("lease_expired") == 1


def test_completion_retry_is_exactly_once_and_changed_retry_conflicts(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="complete-once", now=0)
    claim = app.api.claim("worker", now=1)
    first = app.api.complete(
        job["job_id"], claim["lease_token"], {"value": [1]}, now=2
    )
    before = app.snapshot()
    repeated = app.api.complete(
        job["job_id"], claim["lease_token"], {"value": [1]}, now=99
    )

    assert repeated == first
    assert app.snapshot() == before
    with pytest.raises(OperationConflict):
        app.api.complete(
            job["job_id"], claim["lease_token"], {"value": [2]}, now=100
        )
    assert app.snapshot() == before
    assert app.api.claim("other", now=100) is None
