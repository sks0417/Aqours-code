from __future__ import annotations

import pytest

from conftest import request
from worker_queue import InvalidStateTransition, StaleLease


def test_basic_claim_and_complete(app):
    job = app.api.submit(request(), request_id="complete", now=0)
    claim = app.api.claim("worker-a", now=1)
    completed = app.api.complete(
        job["job_id"], claim["lease_token"], {"ok": True}, now=2
    )

    assert completed["status"] == "completed"
    assert completed["attempt"] == 1
    assert completed["lease_token"] is None
    assert app.api.claim("worker-b", now=50) is None


def test_expired_worker_is_fenced_after_reclaim(app):
    job = app.api.submit(request(), request_id="fence", now=0)
    old = app.api.claim("worker-a", now=1)
    current = app.api.claim("worker-b", now=11)

    assert current["lease_generation"] == old["lease_generation"] + 1
    before = app.snapshot()
    with pytest.raises(StaleLease):
        app.api.complete(job["job_id"], old["lease_token"], "late", now=12)
    assert app.snapshot() == before


def test_failure_counts_claims_and_exact_retry_is_idempotent(app):
    job = app.api.submit(request(max_attempts=3), request_id="retry", now=0)
    claim = app.api.claim("worker-a", now=1)
    failed = app.api.fail(
        job["job_id"], claim["lease_token"], "temporary",
        retry_at=5, now=2,
    )
    history_size = len(app.api.history(job["job_id"]))
    repeated = app.api.fail(
        job["job_id"], claim["lease_token"], "temporary",
        retry_at=5.0, now=3,
    )

    assert failed["status"] == "retry_waiting"
    assert repeated["attempt"] == 1
    assert len(app.api.history(job["job_id"])) == history_size
    assert app.api.claim("early", now=4) is None
    assert app.api.claim("ready", now=5)["attempt"] == 2


def test_cancel_invalidates_lease_and_terminal_jobs_cannot_be_cancelled(app):
    leased = app.api.submit(request(), request_id="cancel", now=0)
    claim = app.api.claim("worker", now=1)
    cancelled = app.api.cancel(leased["job_id"], now=2)

    assert cancelled["status"] == "cancelled"
    assert cancelled["lease_token"] is None
    assert cancelled["worker_id"] is None
    with pytest.raises(StaleLease):
        app.api.complete(leased["job_id"], claim["lease_token"], "late", now=3)

    completed = app.api.submit(request(), request_id="done", now=4)
    done_claim = app.api.claim("worker", now=5)
    app.api.complete(completed["job_id"], done_claim["lease_token"], "ok", now=6)
    before = app.snapshot()
    with pytest.raises(InvalidStateTransition):
        app.api.cancel(completed["job_id"], now=7)
    assert app.snapshot() == before
