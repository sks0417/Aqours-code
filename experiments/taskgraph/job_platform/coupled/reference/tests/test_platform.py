"""Tests of the job platform features, through the public interface."""
import pytest

from jobrunner.api import handle
from jobrunner.dashboard import render_page
from jobrunner.errors import NotFound, TransientError
from jobrunner.models import JobStatus
from jobrunner.runner import Runner
from jobrunner.store import JobStore


def run_all(runner):
    order = []
    while (job := runner.run_once()) is not None:
        order.append(job.id)
    return order


def test_priority_then_tenant_rotation(runner):
    a1 = runner.submit("echo", {}, tenant="a")
    a2 = runner.submit("echo", {}, tenant="a")
    b1 = runner.submit("echo", {}, tenant="b")
    urgent = runner.submit("echo", {}, tenant="a", priority=5)
    assert run_all(runner) == [urgent.id, b1.id, a1.id, a2.id]
    assert runner.tenants()[0] == {"tenant": "a", "pending": 0, "running": 0, "finished": 3}


def test_delayed_and_recurring_jobs(runner, clock):
    later = runner.submit("echo", {}, run_at=clock.now + 5)
    definition_id = runner.schedule_recurring("echo", {"tick": True}, 10)
    first = runner.run_once()
    assert first.definition_id == definition_id and runner.run_once() is None
    clock.advance(25)
    assert runner.run_once().id == later.id
    assert runner.run_once().definition_id == definition_id
    assert len(runner.recurring_jobs(definition_id)) == 2
    assert runner.get_recurring(definition_id).next_run_at == clock.now + 5
    runner.pause_recurring(definition_id)
    clock.advance(10)
    assert runner.run_once() is None
    with pytest.raises(NotFound):
        runner.get_recurring(999)


def test_dependencies_wait_and_cascade(runner):
    ok = runner.submit("echo", {})
    bad = runner.submit("boom", {})
    after_ok = runner.submit("echo", {}, depends_on=[ok.id])
    after_bad = runner.submit("echo", {}, depends_on=[bad.id])
    chained = runner.submit("echo", {}, depends_on=[after_bad.id])
    assert run_all(runner) == [ok.id, bad.id, after_ok.id]
    for job in (after_bad, chained):
        assert runner.get(job.id).cancel_reason == "dependency_failed"
    assert [job.id for job in runner.dependents(bad.id)] == [after_bad.id]
    with pytest.raises(ValueError):
        runner.submit("echo", {}, depends_on=[999])


def test_rate_limit_skips_only_its_kind(runner, clock):
    runner.set_rate_limit("echo", 1, 10)
    runner.submit("echo", {})
    blocked = runner.submit("echo", {})
    other = runner.submit("boom", {})
    assert run_all(runner)[1] == other.id
    assert runner.get(blocked.id).status is JobStatus.PENDING
    assert runner.list_rate_limits()[0].recent_starts == 1
    clock.advance(10)
    assert runner.run_once().id == blocked.id


def test_notifications_are_delivered_in_order_with_retries(runner, clock):
    runner.subscribe("https://hooks.example", statuses=["running", "succeeded"])
    job = runner.submit("echo", {})
    runner.run_once()
    calls = []

    def flaky(url, payload):
        calls.append(payload["new_status"])
        if len(calls) == 1:
            raise ConnectionError("down")

    assert runner.deliver_notifications(flaky) == 0
    clock.advance(1)
    assert runner.deliver_notifications(flaky) == 2
    assert calls == ["running", "running", "succeeded"]
    assert {note.job_id for note in runner.list_notifications("delivered")} == {job.id}


def test_restart_keeps_platform_state(tmp_path, clock):
    path = tmp_path / "platform.db"
    first = Runner(JobStore(path), {"work": lambda payload: "ok"}, clock)
    first.set_rate_limit("work", 1, 60)
    first.subscribe("https://hooks.example")
    first.submit("work", {}, tenant="t")
    first.run_once()
    second = Runner(JobStore(path), {"work": lambda payload: "ok"}, clock)
    assert second.list_rate_limits()[0].recent_starts == 1
    assert len(second.list_notifications()) == 3
    assert [entry.new_status for entry in second.audit_log()] == ["pending", "running",
                                                                  "succeeded"]
    first.store.close()
    second.store.close()


def test_history_and_stats(runner, clock):
    flaky_calls = []

    def flaky(payload):
        flaky_calls.append(1)
        if len(flaky_calls) == 1:
            raise TransientError("busy")
        return "done"

    runner.handlers["flaky"] = flaky
    job = runner.submit("flaky", {})
    runner.submit("boom", {})
    runner.run_once()
    runner.run_once()
    clock.advance(1)
    runner.run_once()
    reasons = [entry.reason for entry in runner.history(job.id)]
    assert reasons == ["submitted", "started", "busy", "started", "succeeded"]
    stats = runner.stats()
    assert (stats["finished"], stats["failed"], stats["failure_rate"]) == (2, 1, 0.5)
    assert stats["average_attempts"] == 1.5
    assert runner.stats(window_s=0.5)["finished"] == 1


def test_api_and_dashboard_cover_the_features(runner):
    status, job = handle(runner, "POST", "/jobs", {"kind": "echo", "priority": 2})
    assert status == 201 and job["priority"] == 2
    assert handle(runner, "PUT", "/rate-limits/echo", {"max_starts": 3, "window_s": 5})[0] == 200
    assert handle(runner, "POST", "/recurring", {"kind": "echo", "interval_s": 60})[0] == 201
    assert handle(runner, "POST", "/subscriptions", {"url": "https://x"})[0] == 201
    runner.run_once()
    for path in ("/queue", "/tenants", "/recurring", f"/jobs/{job['id']}/dependencies",
                 "/rate-limits", "/subscriptions", "/notifications",
                 f"/jobs/{job['id']}/history", "/audit", "/stats?window_s=10"):
        assert handle(runner, "GET", path)[0] == 200, path
    for path in ("/queue", "/recurring", f"/jobs/{job['id']}/dependencies", "/rate-limits",
                 "/notifications", f"/jobs/{job['id']}/history", "/audit", "/stats"):
        status, html = render_page(runner, path)
        assert status == 200 and html.startswith("<h1>"), path
    assert handle(runner, "GET", "/stats?window_s=x")[0] == 400
