from __future__ import annotations

from conftest import event_types, queue_request
from worker_queue import build_application


def _mixed_application(make_application):
    app = make_application()
    expired = app.api.submit(queue_request(), request_id="expired", now=0)
    held = app.api.submit(queue_request(), request_id="held", now=0)
    future = app.api.submit(queue_request(), request_id="future", now=0)
    done = app.api.submit(queue_request(), request_id="done", now=0)
    cancelled = app.api.submit(queue_request(), request_id="cancelled", now=0)
    app.api.claim("old", now=1)
    app.api.claim("held", now=8)
    retry_claim = app.api.claim("retry", now=9)
    app.api.fail(
        future["job_id"], retry_claim["lease_token"], "busy", retry_at=30, now=9.5
    )
    done_claim = app.api.claim("done", now=10)
    app.api.complete(done["job_id"], done_claim["lease_token"], {"ok": True}, now=10.5)
    app.api.cancel(cancelled["job_id"], now=10.6)
    return app, expired, held, future, done, cancelled


def test_recovery_changes_only_expired_leases_and_preserves_fields(make_application):
    app, expired, held, future, done, cancelled = _mixed_application(make_application)
    before = {job["job_id"]: job for job in app.api.list_jobs()}
    restarted = build_application(app.snapshot(), lease_seconds=10)
    report = restarted.api.recover(now=12)

    assert report == {
        "expired_leases": 1,
        "runnable_jobs": 1,
        "held_leases": 1,
        "terminal_jobs": 2,
        "recovered_job_ids": [expired["job_id"]],
    }
    assert restarted.api.get_job(expired["job_id"])["status"] == "pending"
    for job in (held, future, done, cancelled):
        after = restarted.api.get_job(job["job_id"])
        original = before[job["job_id"]]
        assert after == original


def test_repeated_recovery_is_state_and_history_idempotent(make_application):
    app = make_application()
    job = app.api.submit(queue_request(), request_id="repeat-recovery", now=0)
    app.api.claim("worker", now=1)
    restarted = build_application(app.snapshot(), lease_seconds=10)

    first = restarted.api.recover(now=11)
    after_first = restarted.snapshot()
    second = restarted.api.recover(now=11)

    assert first["recovered_job_ids"] == [job["job_id"]]
    assert second["recovered_job_ids"] == []
    assert second["expired_leases"] == 0
    assert restarted.snapshot() == after_first
    assert event_types(restarted.api, job["job_id"]).count("lease_expired") == 1


def test_recovery_preserves_order_attempts_and_retry_availability(make_application):
    app = make_application()
    first = app.api.submit(queue_request(), request_id="first", now=0)
    second = app.api.submit(queue_request(), request_id="second", now=0)
    third = app.api.submit(queue_request(), request_id="third", now=0)
    old = app.api.claim("worker", now=1)
    second_claim = app.api.claim("worker", now=2)
    app.api.fail(
        second["job_id"], second_claim["lease_token"], "wait", retry_at=50, now=3
    )
    restarted = build_application(app.snapshot(), lease_seconds=10)
    restarted.api.recover(now=11)

    replacement = restarted.api.claim("new", now=11)
    assert replacement["job_id"] == first["job_id"]
    assert replacement["attempt"] == old["attempt"] + 1
    restarted.api.complete(
        first["job_id"], replacement["lease_token"], "first done", now=11.5
    )
    next_claim = restarted.api.claim("next", now=12)
    assert next_claim["job_id"] == third["job_id"]
    restarted.api.complete(
        third["job_id"], next_claim["lease_token"], "third done", now=13
    )
    assert restarted.api.claim("future", now=49) is None
    assert restarted.api.claim("due", now=50)["job_id"] == second["job_id"]


def test_sequences_ids_and_receipts_continue_across_snapshot(make_application):
    app = make_application()
    first = app.api.submit(queue_request(), request_id="first-id", now=0)
    claim = app.api.claim("worker", now=1)
    app.api.complete(first["job_id"], claim["lease_token"], "ok", now=2)
    restarted = build_application(app.snapshot(), lease_seconds=10)
    before_retry = restarted.snapshot()
    restarted.api.complete(first["job_id"], claim["lease_token"], "ok", now=100)
    assert restarted.snapshot() == before_retry

    second = restarted.api.submit(queue_request(), request_id="second-id", now=3)
    second_claim = restarted.api.claim("worker", now=4)
    assert second["job_id"] == "job-000002"
    assert second_claim["lease_token"] != claim["lease_token"]
    sequences = [event["sequence"] for event in restarted.api.history()]
    assert sequences == list(range(1, len(sequences) + 1))
