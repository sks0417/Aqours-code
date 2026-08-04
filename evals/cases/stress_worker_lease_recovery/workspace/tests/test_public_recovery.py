from __future__ import annotations

from conftest import request
from worker_queue import build_application


def test_restart_recovery_releases_only_expired_leases(app):
    expired = app.api.submit(request(), request_id="expired", now=0)
    held = app.api.submit(request(), request_id="held", now=0)
    terminal = app.api.submit(request(), request_id="terminal", now=0)

    app.api.claim("old", now=1)
    app.api.claim("current", now=8)
    terminal_claim = app.api.claim("finisher", now=9)
    app.api.complete(
        terminal["job_id"], terminal_claim["lease_token"], {"saved": True}, now=10
    )
    restarted = build_application(app.snapshot(), lease_seconds=10)
    report = restarted.api.recover(now=12)

    assert report == {
        "expired_leases": 1,
        "runnable_jobs": 1,
        "held_leases": 1,
        "terminal_jobs": 1,
        "recovered_job_ids": [expired["job_id"]],
    }
    assert restarted.api.get_job(held["job_id"])["status"] == "leased"
    assert restarted.api.get_job(terminal["job_id"])["status"] == "completed"
    assert restarted.api.get_job(terminal["job_id"])["result"] == {"saved": True}

    after_first = restarted.snapshot()
    restarted.api.recover(now=12)
    assert restarted.snapshot() == after_first
