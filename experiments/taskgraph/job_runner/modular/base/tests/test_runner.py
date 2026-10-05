import pytest

from jobrunner.errors import JobNotFound
from jobrunner.models import JobStatus


def test_submit_creates_pending_job(runner, clock):
    job = runner.submit("echo", {"x": 1})
    assert job.status is JobStatus.PENDING
    assert job.created_at == clock.now
    assert runner.get(job.id).payload == {"x": 1}


def test_submit_unknown_kind_is_rejected(runner):
    with pytest.raises(ValueError):
        runner.submit("missing", {})


def test_run_once_runs_job_and_stores_result(runner):
    job = runner.submit("echo", {"x": 1})
    done = runner.run_once()
    assert done.id == job.id
    assert done.status is JobStatus.SUCCEEDED
    assert runner.get(job.id).result == {"x": 1}


def test_run_once_without_jobs_returns_none(runner):
    assert runner.run_once() is None


def test_jobs_run_in_submission_order(runner, clock):
    first = runner.submit("echo", {"n": 1})
    clock.advance(1)
    second = runner.submit("echo", {"n": 2})
    assert runner.run_once().id == first.id
    assert runner.run_once().id == second.id
    assert runner.run_once() is None


def test_handler_error_fails_job(runner):
    job = runner.submit("boom", {})
    assert runner.run_once().status is JobStatus.FAILED
    assert runner.get(job.id).status is JobStatus.FAILED
    assert runner.run_once() is None


def test_get_unknown_job_raises(runner):
    with pytest.raises(JobNotFound):
        runner.get(999)
