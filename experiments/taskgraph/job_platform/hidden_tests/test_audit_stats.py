import pytest

from conftest import Crash, Script

STATUSES = ("pending", "running", "succeeded", "failed", "cancelled")


def changes(runner, job):
    return [(e.old_status, e.new_status, e.reason) for e in runner.history(job.id)]


def test_history_of_a_successful_job(make_runner, clock):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    clock.advance(3)
    runner.run_once()
    entries = runner.history(job.id)
    assert changes(runner, job) == [(None, "pending", "submitted"),
                                    ("pending", "running", "started"),
                                    ("running", "succeeded", "succeeded")]
    assert [e.time for e in entries] == [1000.0, 1003.0, 1003.0]
    assert all(e.job_id == job.id for e in entries)
    assert entries[1].to_dict() == {"job_id": job.id, "old_status": "pending",
                                    "new_status": "running", "time": 1003.0,
                                    "reason": "started"}


def test_history_of_a_retried_then_failed_job(make_runner, clock, jr):
    runner = make_runner({"work": Script(jr.errors.TransientError("busy"),
                                         ValueError("broken"))})
    job = runner.submit("work", {})
    runner.run_once()
    clock.advance(1)
    runner.run_once()
    assert changes(runner, job) == [(None, "pending", "submitted"),
                                    ("pending", "running", "started"),
                                    ("running", "pending", "busy"),
                                    ("pending", "running", "started"),
                                    ("running", "failed", "broken")]


def test_history_of_cancellations(make_runner):
    box = {}

    def cancel_myself():
        box["runner"].cancel(box["id"])
        return "ignored"

    runner = make_runner({"work": Script(cancel_myself)})
    box["runner"] = runner
    running = runner.submit("work", {})
    box["id"] = running.id
    waiting = runner.submit("work", {})
    runner.cancel(waiting.id)
    runner.run_once()
    assert changes(runner, waiting)[-1] == ("pending", "cancelled", "cancel_requested")
    assert changes(runner, running)[1:] == [("pending", "running", "started"),
                                           ("running", "cancelled", "cancel_requested")]


def test_history_of_restart_recovery(make_runner):
    runner = make_runner({"work": Script(Crash())})
    job = runner.submit("work", {})
    with pytest.raises(Crash):
        runner.run_once()
    restarted = make_runner({"work": Script()})
    assert changes(restarted, job)[-1] == ("running", "pending",
                                           "interrupted by a process restart")
    assert len(restarted.history(job.id)) == 3


def test_history_of_unknown_job(make_runner, jr):
    runner = make_runner({"work": Script()})
    with pytest.raises(jr.errors.JobNotFound):
        runner.history(999)


def test_priority_change_is_not_a_state_change(make_runner):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    runner.set_priority(job.id, 4)
    assert len(runner.history(job.id)) == 1


def test_stats_on_an_empty_platform(make_runner):
    runner = make_runner({"work": Script()})
    stats = runner.stats()
    assert stats == {"window_s": None, "by_status": {status: 0 for status in STATUSES},
                     "by_kind": {}, "by_tenant": {}, "finished": 0, "failed": 0, "failure_rate": 0.0,
                     "average_attempts": 0.0}


def test_stats_counts_jobs_by_status_and_kind(make_runner):
    runner = make_runner({"ok": Script(), "bad": Script(ValueError("x")), "idle": Script()})
    runner.submit("ok", {}, tenant="t2")
    runner.submit("bad", {})
    idle = runner.submit("idle", {}, tenant="t1")
    runner.submit("idle", {}, tenant="t2")
    runner.run_once()
    runner.run_once()
    runner.cancel(idle.id)
    stats = runner.stats()
    assert stats["by_status"] == {"pending": 1, "running": 0, "succeeded": 1, "failed": 1,
                                  "cancelled": 1}
    assert stats["by_kind"] == {"bad": 1, "idle": 2, "ok": 1}
    assert list(stats["by_tenant"].items()) == [("default", 1), ("t1", 1), ("t2", 2)]


def test_failure_rate_and_average_attempts(make_runner, clock, jr):
    def broken(payload):
        raise ValueError("x")

    runner = make_runner({"ok": Script(jr.errors.TransientError("busy")), "bad": broken})
    runner.submit("ok", {})
    runner.submit("bad", {})
    runner.submit("bad", {})
    runner.run_once()                # "ok" fails once and retries at now + 1
    runner.run_once()
    runner.run_once()
    clock.advance(1)
    runner.run_once()                # "ok" succeeds on its second attempt
    stats = runner.stats()
    assert (stats["finished"], stats["failed"]) == (3, 2)
    assert stats["failure_rate"] == pytest.approx(2 / 3)
    assert stats["average_attempts"] == pytest.approx(4 / 3)


def test_stats_window_counts_recent_finishes_only(make_runner, clock):
    runner = make_runner({"ok": Script(), "bad": Script(ValueError("x"))})
    runner.submit("bad", {})
    runner.run_once()                # failed at 1000
    clock.advance(50)
    runner.submit("ok", {})
    runner.run_once()                # succeeded at 1050
    clock.advance(50)                # now 1100
    assert runner.stats(window_s=50)["finished"] == 0   # 1050 is not inside (1050, 1100]
    recent = runner.stats(window_s=60)
    assert recent["window_s"] == 60
    assert (recent["finished"], recent["failed"], recent["failure_rate"]) == (1, 0, 0.0)
    everything = runner.stats()
    assert (everything["finished"], everything["failure_rate"]) == (2, 0.5)
    assert runner.stats(window_s=60)["by_status"]["failed"] == 1


def test_invalid_window_is_rejected(make_runner):
    runner = make_runner({"work": Script()})
    for window in (0, -5, "60", True):
        with pytest.raises(ValueError):
            runner.stats(window_s=window)


def test_audit_log_survives_a_restart(make_runner, clock):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    runner.run_once()
    restarted = make_runner({"work": Script()})
    assert len(restarted.history(job.id)) == 3
    assert restarted.stats()["finished"] == 1


def test_audit_log_lists_every_change_in_order(make_runner, clock):
    runner = make_runner({"work": Script()})
    first = runner.submit("work", {})
    clock.advance(1)
    second = runner.submit("work", {})
    runner.run_once()
    logged = [(entry.job_id, entry.new_status, entry.time) for entry in runner.audit_log()]
    assert logged == [(first.id, "pending", 1000.0), (second.id, "pending", 1001.0),
                      (first.id, "running", 1001.0), (first.id, "succeeded", 1001.0)]
    recent = runner.audit_log(since=1000.0)
    assert [(entry.job_id, entry.new_status) for entry in recent] == [
        (second.id, "pending"), (first.id, "running"), (first.id, "succeeded")]
    assert runner.audit_log(since=1001.0) == []
    with pytest.raises(ValueError):
        runner.audit_log(since="yesterday")
