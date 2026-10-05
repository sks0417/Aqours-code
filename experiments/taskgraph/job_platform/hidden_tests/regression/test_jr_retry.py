from conftest import Script


def transient(jr, message="try later"):
    return jr.errors.TransientError(message)


def test_transient_failure_returns_job_to_pending_with_backoff(make_runner, clock, jr):
    runner = make_runner({"work": Script(transient(jr, "boom"))})
    job = runner.submit("work", {})
    assert job.attempts == 0 and job.max_attempts == 3 and job.next_run_at == clock.now
    done = runner.run_once()
    assert done.status == jr.models.JobStatus.PENDING
    stored = runner.get(job.id)
    assert stored.status == jr.models.JobStatus.PENDING
    assert stored.attempts == 1
    assert stored.next_run_at == clock.now + 1.0
    assert stored.last_error == "boom"


def test_job_does_not_run_before_next_run_at(make_runner, clock, jr):
    work = Script(transient(jr), "done")
    runner = make_runner({"work": work})
    job = runner.submit("work", {})
    runner.run_once()
    clock.advance(0.5)
    assert runner.run_once() is None
    clock.advance(0.5)
    assert runner.run_once().id == job.id
    assert runner.get(job.id).status == jr.models.JobStatus.SUCCEEDED
    assert work.calls == 2


def test_backoff_doubles_after_each_failure(make_runner, clock, jr):
    runner = make_runner({"work": Script(transient(jr), transient(jr), transient(jr))},
                         max_attempts=4, base_delay=2.0)
    job = runner.submit("work", {})
    delays = []
    for _ in range(3):
        runner.run_once()
        delays.append(runner.get(job.id).next_run_at - clock.now)
        clock.advance(delays[-1])
    assert delays == [2.0, 4.0, 8.0]
    assert runner.get(job.id).attempts == 3


def test_job_fails_after_max_attempts(make_runner, clock, jr):
    work = Script(transient(jr, "one"), transient(jr, "two"), transient(jr, "three"))
    runner = make_runner({"work": work})
    job = runner.submit("work", {})
    for _ in range(3):
        runner.run_once()
        clock.advance(10)
    stored = runner.get(job.id)
    assert stored.status == jr.models.JobStatus.FAILED
    assert stored.attempts == 3 and stored.last_error == "three"
    assert runner.run_once() is None
    assert work.calls == 3


def test_other_errors_fail_without_retry(make_runner, clock, jr):
    work = Script(ValueError("bad payload"))
    runner = make_runner({"work": work})
    job = runner.submit("work", {})
    runner.run_once()
    stored = runner.get(job.id)
    assert stored.status == jr.models.JobStatus.FAILED
    assert stored.attempts == 1 and stored.last_error == "bad payload"
    clock.advance(100)
    assert runner.run_once() is None and work.calls == 1


def test_job_succeeds_after_a_retry(make_runner, clock, jr):
    runner = make_runner({"work": Script(transient(jr), {"ok": True})})
    job = runner.submit("work", {})
    runner.run_once()
    clock.advance(1)
    done = runner.run_once()
    assert done.status == jr.models.JobStatus.SUCCEEDED
    stored = runner.get(job.id)
    assert stored.result == {"ok": True} and stored.attempts == 2


def test_due_jobs_run_by_next_run_at_then_age(make_runner, clock, jr):
    runner = make_runner({"work": Script(transient(jr)), "other": Script()})
    retried = runner.submit("work", {})
    runner.run_once()  # retried is due again at 1001.0
    clock.advance(0.5)
    first = runner.submit("other", {})  # due at 1000.5
    second = runner.submit("other", {})  # due at 1000.5, created later
    clock.advance(5)
    order = [runner.run_once().id for _ in range(3)]
    assert order == [first.id, second.id, retried.id]
