import pytest

from conftest import Script


def cancelling(holder, runner_box, then=None):
    """A handler step that cancels its own job while running."""
    def step():
        cancelled = runner_box["runner"].cancel(holder["id"])
        holder["seen"] = cancelled
        return then
    return step


def test_cancel_pending_job(make_runner, jr):
    work = Script()
    runner = make_runner({"work": work})
    job = runner.submit("work", {})
    cancelled = runner.cancel(job.id)
    assert cancelled.status == jr.models.JobStatus.CANCELLED
    assert runner.get(job.id).status == jr.models.JobStatus.CANCELLED
    assert runner.run_once() is None and work.calls == 0


def test_cancel_running_job_discards_result(make_runner, jr):
    holder, box = {}, {}
    runner = make_runner({"work": Script(cancelling(holder, box, then={"done": 1}))})
    box["runner"] = runner
    job = runner.submit("work", {})
    holder["id"] = job.id
    done = runner.run_once()
    assert holder["seen"].status == jr.models.JobStatus.RUNNING
    assert holder["seen"].cancel_requested is True
    assert done.status == jr.models.JobStatus.CANCELLED
    stored = runner.get(job.id)
    assert stored.status == jr.models.JobStatus.CANCELLED and stored.result is None


def test_cancel_running_job_that_fails_is_not_retried(make_runner, clock, jr):
    holder, box = {}, {}

    def cancel_then_fail():
        cancelling(holder, box)()
        return jr.errors.TransientError("flaky")

    work = Script(cancel_then_fail)
    runner = make_runner({"work": work})
    box["runner"] = runner
    job = runner.submit("work", {})
    holder["id"] = job.id
    runner.run_once()
    assert runner.get(job.id).status == jr.models.JobStatus.CANCELLED
    clock.advance(100)
    assert runner.run_once() is None and work.calls == 1


def test_cancel_job_waiting_for_retry(make_runner, clock, jr):
    work = Script(jr.errors.TransientError("flaky"))
    runner = make_runner({"work": work})
    job = runner.submit("work", {})
    runner.run_once()
    assert runner.cancel(job.id).status == jr.models.JobStatus.CANCELLED
    clock.advance(100)
    assert runner.run_once() is None and work.calls == 1


def test_cancel_finished_jobs_is_invalid(make_runner, jr):
    runner = make_runner({"ok": Script("fine"), "bad": Script(ValueError("no"))})
    succeeded = runner.submit("ok", {})
    failed = runner.submit("bad", {})
    runner.run_once()
    runner.run_once()
    pending = runner.submit("ok", {})
    runner.cancel(pending.id)
    for job in (succeeded, failed, pending):
        with pytest.raises(jr.errors.InvalidTransition):
            runner.cancel(job.id)


def test_cancel_unknown_job(make_runner, jr):
    runner = make_runner({"ok": Script()})
    with pytest.raises(jr.errors.JobNotFound):
        runner.cancel(12345)
