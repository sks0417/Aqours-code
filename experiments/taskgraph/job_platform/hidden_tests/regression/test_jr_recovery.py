import pytest

from conftest import Crash, Script


def test_retry_schedule_survives_restart(make_runner, clock, jr):
    runner = make_runner({"work": Script(jr.errors.TransientError("flaky"))})
    job = runner.submit("work", {})
    runner.run_once()
    restarted = make_runner({"work": Script({"ok": 1})})
    stored = restarted.get(job.id)
    assert stored.attempts == 1 and stored.last_error == "flaky"
    assert stored.next_run_at == clock.now + 1.0
    assert restarted.run_once() is None
    clock.advance(1)
    assert restarted.run_once().status == jr.models.JobStatus.SUCCEEDED


def test_interrupted_job_is_retried_after_restart(make_runner, clock, jr):
    runner = make_runner({"work": Script(Crash())})
    job = runner.submit("work", {})
    with pytest.raises(Crash):
        runner.run_once()
    clock.advance(10)
    work = Script({"ok": 1})
    restarted = make_runner({"work": work})
    stored = restarted.get(job.id)
    assert stored.status == jr.models.JobStatus.PENDING
    assert stored.attempts == 1
    assert stored.next_run_at == clock.now + 1.0
    assert stored.last_error
    clock.advance(1)
    assert restarted.run_once().status == jr.models.JobStatus.SUCCEEDED
    assert work.calls == 1 and restarted.get(job.id).attempts == 2


def test_interrupted_job_uses_backoff_for_its_attempt(make_runner, clock, jr):
    runner = make_runner({"work": Script(jr.errors.TransientError("flaky"), Crash())},
                         base_delay=2.0)
    job = runner.submit("work", {})
    runner.run_once()
    clock.advance(2)
    with pytest.raises(Crash):
        runner.run_once()
    restarted = make_runner({"work": Script()}, base_delay=2.0)
    stored = restarted.get(job.id)
    assert stored.attempts == 2
    assert stored.next_run_at == clock.now + 4.0


def test_interrupted_job_out_of_attempts_fails(make_runner, jr):
    runner = make_runner({"work": Script(Crash())}, max_attempts=1)
    job = runner.submit("work", {})
    with pytest.raises(Crash):
        runner.run_once()
    restarted = make_runner({"work": Script()}, max_attempts=1)
    stored = restarted.get(job.id)
    assert stored.status == jr.models.JobStatus.FAILED
    assert stored.attempts == 1 and stored.last_error
    assert restarted.run_once() is None


def test_interrupted_job_with_cancel_request_is_cancelled(make_runner, clock, jr):
    box = {}

    def cancel_then_crash():
        box["runner"].cancel(box["id"])
        return Crash()

    runner = make_runner({"work": Script(cancel_then_crash)})
    box["runner"] = runner
    job = runner.submit("work", {})
    box["id"] = job.id
    with pytest.raises(Crash):
        runner.run_once()
    work = Script()
    restarted = make_runner({"work": work})
    assert restarted.get(job.id).status == jr.models.JobStatus.CANCELLED
    clock.advance(100)
    assert restarted.run_once() is None and work.calls == 0

