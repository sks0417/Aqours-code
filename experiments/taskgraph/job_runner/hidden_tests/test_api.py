from conftest import Script

JOB_FIELDS = ("id", "kind", "status", "attempts", "max_attempts", "next_run_at",
              "last_error", "cancel_requested")


def test_created_job_has_status_fields(make_runner, clock, jr):
    runner = make_runner({"work": Script()})
    status, body = jr.api.handle(runner, "POST", "/jobs", {"kind": "work", "payload": {}})
    assert status == 201
    for field in JOB_FIELDS:
        assert field in body
    assert body["status"] == "pending" and body["attempts"] == 0
    assert body["max_attempts"] == 3 and body["next_run_at"] == clock.now
    assert body["last_error"] is None and body["cancel_requested"] is False


def test_retry_status_is_visible(make_runner, clock, jr):
    runner = make_runner({"work": Script(jr.errors.TransientError("flaky"))})
    job = runner.submit("work", {})
    runner.run_once()
    status, body = jr.api.handle(runner, "GET", f"/jobs/{job.id}")
    assert status == 200
    assert body["status"] == "pending" and body["attempts"] == 1
    assert body["last_error"] == "flaky" and body["next_run_at"] == clock.now + 1.0


def test_cancel_endpoint(make_runner, jr):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    status, body = jr.api.handle(runner, "POST", f"/jobs/{job.id}/cancel")
    assert status == 200 and body["status"] == "cancelled"
    assert runner.get(job.id).status == jr.models.JobStatus.CANCELLED


def test_cancel_finished_job_is_conflict(make_runner, jr):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    runner.run_once()
    status, body = jr.api.handle(runner, "POST", f"/jobs/{job.id}/cancel")
    assert status == 409 and "error" in body
    status, _ = jr.api.handle(runner, "POST", f"/jobs/{job.id}/cancel")
    assert status == 409


def test_cancel_unknown_job_is_404(make_runner, jr):
    runner = make_runner({"work": Script()})
    status, body = jr.api.handle(runner, "POST", "/jobs/4242/cancel")
    assert status == 404 and "error" in body


def test_list_cancelled_jobs(make_runner, jr):
    runner = make_runner({"work": Script()})
    keep = runner.submit("work", {})
    drop = runner.submit("work", {})
    runner.cancel(drop.id)
    status, body = jr.api.handle(runner, "GET", "/jobs?status=cancelled")
    assert status == 200 and [job["id"] for job in body["jobs"]] == [drop.id]
    _, pending = jr.api.handle(runner, "GET", "/jobs?status=pending")
    assert [job["id"] for job in pending["jobs"]] == [keep.id]
