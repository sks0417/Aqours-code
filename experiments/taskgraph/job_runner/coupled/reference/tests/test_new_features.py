import pytest

from jobrunner.api import handle
from jobrunner.dashboard import render_jobs
from jobrunner.errors import InvalidTransition, TransientError
from jobrunner.models import JobStatus
from jobrunner.runner import Runner
from jobrunner.store import JobStore


class Flaky:
    def __init__(self, failures):
        self.failures = failures

    def __call__(self, payload):
        if self.failures:
            self.failures -= 1
            raise TransientError("flaky")
        return "done"


class Crash(BaseException):
    pass


def crash(payload):
    raise Crash()


def test_transient_failures_are_retried_with_backoff(store, clock):
    runner = Runner(store, {"work": Flaky(2)}, clock, base_delay=1.0)
    job = runner.submit("work", {})
    runner.run_once()
    assert runner.get(job.id).next_run_at == clock.now + 1.0
    clock.advance(1)
    runner.run_once()
    assert runner.get(job.id).next_run_at == clock.now + 2.0
    assert runner.run_once() is None
    clock.advance(2)
    assert runner.run_once().status is JobStatus.SUCCEEDED
    assert runner.get(job.id).attempts == 3


def test_retries_stop_at_max_attempts(store, clock):
    runner = Runner(store, {"work": Flaky(5)}, clock, max_attempts=2)
    job = runner.submit("work", {})
    runner.run_once()
    clock.advance(10)
    runner.run_once()
    assert runner.get(job.id).status is JobStatus.FAILED
    assert runner.get(job.id).last_error == "flaky"


def test_cancel_pending_and_finished(runner):
    job = runner.submit("echo", {})
    assert runner.cancel(job.id).status is JobStatus.CANCELLED
    assert runner.run_once() is None
    with pytest.raises(InvalidTransition):
        runner.cancel(job.id)


def test_restart_retries_interrupted_job(tmp_path, clock):
    path = tmp_path / "restart.db"
    first = Runner(JobStore(path), {"work": crash}, clock)
    job = first.submit("work", {})
    with pytest.raises(Crash):
        first.run_once()
    second = Runner(JobStore(path), {"work": Flaky(0)}, clock)
    recovered = second.get(job.id)
    assert recovered.status is JobStatus.PENDING and recovered.attempts == 1
    clock.advance(1)
    assert second.run_once().status is JobStatus.SUCCEEDED
    first.store.close()
    second.store.close()


def test_api_and_dashboard_show_status(runner):
    job = runner.submit("echo", {})
    html = render_jobs([runner.get(job.id)])
    assert f'action="/jobs/{job.id}/cancel"' in html and "<td>0/3</td>" in html
    status, body = handle(runner, "POST", f"/jobs/{job.id}/cancel")
    assert status == 200 and body["status"] == "cancelled"
    assert handle(runner, "POST", f"/jobs/{job.id}/cancel")[0] == 409
    assert handle(runner, "POST", "/jobs/999/cancel")[0] == 404
