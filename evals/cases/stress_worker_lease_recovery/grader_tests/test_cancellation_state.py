from __future__ import annotations

import pytest

from conftest import event_types, queue_request
from worker_queue import InvalidStateTransition, StaleLease


def test_pending_cancellation_is_idempotent_and_not_claimable(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="pending-cancel", now=0)
    first = app.api.cancel(job["job_id"], now=1)
    before = app.snapshot()
    repeated = app.api.cancel(job["job_id"], now=99)

    assert first == repeated
    assert app.snapshot() == before
    assert event_types(app.api, job["job_id"]).count("cancelled") == 1
    assert app.api.claim("worker", now=100) is None


def test_leased_cancellation_clears_every_capability_field(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="leased-cancel", now=0)
    claim = app.api.claim("worker", now=1)
    cancelled = app.api.cancel(job["job_id"], now=2)

    assert cancelled["status"] == "cancelled"
    assert cancelled["worker_id"] is None
    assert cancelled["lease_token"] is None
    assert cancelled["lease_expires_at"] is None
    assert cancelled["retry_at"] is None
    before = app.snapshot()
    with pytest.raises(StaleLease):
        app.api.fail(
            job["job_id"], claim["lease_token"], "late", retry_at=9, now=3
        )
    assert app.snapshot() == before


def test_retry_waiting_job_can_be_cancelled_and_never_reappears(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="retry-cancel", now=0)
    claim = app.api.claim("worker", now=1)
    app.api.fail(job["job_id"], claim["lease_token"], "busy", retry_at=4, now=2)
    cancelled = app.api.cancel(job["job_id"], now=3)

    assert cancelled["status"] == "cancelled"
    assert cancelled["retry_at"] is None
    app.api.recover(now=100)
    assert app.api.claim("worker", now=100) is None


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_completed_and_failed_jobs_reject_cancellation_without_mutation(
    make_application, terminal
):
    app = make_application()
    job = app.api.submit(
        queue_request(max_attempts=1), request_id=terminal, now=0
    )
    claim = app.api.claim("worker", now=1)
    if terminal == "completed":
        app.api.complete(job["job_id"], claim["lease_token"], "ok", now=2)
    else:
        app.api.fail(job["job_id"], claim["lease_token"], "fatal", retry_at=3, now=2)
    before = app.snapshot()

    with pytest.raises(InvalidStateTransition):
        app.api.cancel(job["job_id"], now=4)
    assert app.snapshot() == before
