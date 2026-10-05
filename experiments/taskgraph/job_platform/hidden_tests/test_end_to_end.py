import pytest

from conftest import Crash, Recorder, Script, table_rows


def post(jr, runner, path, body=None):
    status, reply = jr.api.handle(runner, "POST", path, body)
    assert status in (200, 201), (path, reply)
    return reply


def get(jr, runner, path):
    status, reply = jr.api.handle(runner, "GET", path)
    assert status == 200, (path, reply)
    return reply


def run_all(runner):
    order = []
    while (job := runner.run_once()) is not None:
        order.append(job.id)
    return order


def test_pipeline_with_tenants_priorities_dependencies_and_webhooks(make_runner, jr):
    runner = make_runner({"etl": Script(), "report": Script()})
    post(jr, runner, "/subscriptions", {"url": "https://hooks.example", "statuses": ["succeeded"]})
    extract = post(jr, runner, "/jobs", {"kind": "etl", "tenant": "a"})
    transform = post(jr, runner, "/jobs", {"kind": "etl", "tenant": "a", "priority": 5,
                                           "depends_on": [extract["id"]]})
    load = post(jr, runner, "/jobs", {"kind": "etl", "tenant": "a",
                                      "depends_on": [transform["id"]]})
    report = post(jr, runner, "/jobs", {"kind": "report", "tenant": "b"})
    expected = [extract["id"], transform["id"], report["id"], load["id"]]
    assert run_all(runner) == expected
    sender = Recorder()
    assert runner.deliver_notifications(sender) == 4
    assert [job_id for job_id, _ in sender.events()] == expected
    delivered = get(jr, runner, "/notifications?status=delivered")["notifications"]
    assert [n["job_id"] for n in delivered] == expected
    html = jr.dashboard.render_page(runner, f"/jobs/{transform['id']}/dependencies")[1]
    assert table_rows(html, "dependencies") == [
        ["depends on", str(extract["id"]), "etl", "succeeded"],
        ["dependent", str(load["id"]), "etl", "succeeded"]]


def test_rate_limited_high_priority_job_does_not_block_others(make_runner, clock, jr):
    runner = make_runner({"mail": Script(), "report": Script()})
    jr.api.handle(runner, "PUT", "/rate-limits/mail", {"max_starts": 1, "window_s": 60})
    runner.submit("mail", {})
    urgent = runner.submit("mail", {}, priority=9)
    report = runner.submit("report", {})
    assert runner.run_once().id == urgent.id
    assert runner.run_once().id == report.id
    assert runner.run_once() is None
    assert [job["status"] for job in get(jr, runner, "/queue")["jobs"]] == ["pending"]
    clock.advance(60)
    assert runner.run_once() is not None


def test_dependency_cancellation_is_audited_notified_and_counted(make_runner, jr):
    runner = make_runner({"bad": Script(ValueError("disk full")), "work": Script()})
    runner.subscribe("https://hooks.example", statuses=["cancelled"])
    root = runner.submit("bad", {})
    child = post(jr, runner, "/jobs", {"kind": "work", "depends_on": [root.id]})
    runner.run_once()
    history = get(jr, runner, f"/jobs/{child['id']}/history")["history"]
    assert history[-1]["reason"] == "dependency_failed"
    assert history[-1]["old_status"] == "pending" and history[-1]["new_status"] == "cancelled"
    sender = Recorder()
    runner.deliver_notifications(sender)
    assert [(p["job_id"], p["reason"]) for _, p in sender.calls] == [
        (child["id"], "dependency_failed")]
    stats = get(jr, runner, "/stats")
    assert stats["by_status"]["cancelled"] == 1 and stats["by_status"]["failed"] == 1
    assert (stats["finished"], stats["failed"]) == (1, 1)
    assert get(jr, runner, f"/jobs/{child['id']}")["cancel_reason"] == "dependency_failed"


def test_recurring_job_under_a_rate_limit_skips_busy_periods(make_runner, clock, jr):
    runner = make_runner({"sync": Script()})
    runner.set_rate_limit("sync", 1, 30)
    definition = post(jr, runner, "/recurring", {"kind": "sync", "interval_s": 10})
    first = runner.run_once()                     # t = 1000
    clock.advance(10)
    assert runner.run_once() is None              # t = 1010: job created, rate-limited
    clock.advance(10)
    assert runner.run_once() is None              # t = 1020: last job unfinished, no new job
    clock.advance(10)
    second = runner.run_once()                    # t = 1030: the 1010 job runs
    jobs = [job for job in runner.list() if job.definition_id == definition["id"]]
    assert [job.id for job in jobs] == [first.id, second.id]
    assert second.created_at == 1010.0
    assert get(jr, runner, f"/recurring/{definition['id']}")["next_run_at"] == 1040.0


def test_restart_keeps_the_whole_platform(make_runner, clock, jr):
    runner = make_runner({"work": Script(Crash()), "tick": Script()})
    runner.set_rate_limit("work", 5, 100)
    definition_id = runner.schedule_recurring("tick", {}, 60, start_at=clock.now + 60)
    runner.pause_recurring(definition_id)
    sub = runner.subscribe("https://hooks.example", kinds=["work"])
    job = runner.submit("work", {}, priority=2, tenant="acme")
    with pytest.raises(Crash):
        runner.run_once()
    restarted = make_runner({"work": Script("done"), "tick": Script()})
    assert get(jr, restarted, "/rate-limits")["rate_limits"][0]["recent_starts"] == 1
    assert get(jr, restarted, "/recurring")["recurring"][0]["paused"] is True
    assert get(jr, restarted, "/subscriptions")["subscriptions"][0]["id"] == sub.id
    events = [n["payload"]["new_status"]
              for n in get(jr, restarted, "/notifications")["notifications"]]
    assert events == ["pending", "running", "pending"]
    reasons = [e["reason"] for e in get(jr, restarted, f"/jobs/{job.id}/history")["history"]]
    assert reasons == ["submitted", "started", "interrupted by a process restart"]
    clock.advance(1)
    done = restarted.run_once()
    assert (done.id, done.status.value, done.attempts) == (job.id, "succeeded", 2)
    assert (done.priority, done.tenant) == (2, "acme")
    assert restarted.deliver_notifications(Recorder()) == 5


def test_recurring_jobs_are_audited_and_notified(make_runner, clock, jr):
    runner = make_runner({"tick": Script(), "work": Script()})
    runner.subscribe("https://hooks.example", kinds=["tick"], statuses=["pending"])
    definition_id = runner.schedule_recurring("tick", {"x": 1}, 5)
    runner.submit("work", {})
    run_all(runner)
    clock.advance(5)
    run_all(runner)
    created = [job for job in runner.list() if job.definition_id == definition_id]
    assert len(created) == 2
    for job in created:
        assert runner.history(job.id)[0].reason == "submitted"
    sender = Recorder()
    runner.deliver_notifications(sender)
    assert sender.events() == [(created[0].id, "pending"), (created[1].id, "pending")]
    assert get(jr, runner, "/stats")["by_kind"] == {"tick": 2, "work": 1}


def test_fair_scheduling_between_a_recurring_tenant_and_a_busy_tenant(make_runner, clock):
    runner = make_runner({"tick": Script(), "work": Script()})
    web = [runner.submit("work", {}, tenant="web") for _ in range(3)]
    definition_id = runner.schedule_recurring("tick", {}, 1, tenant="cron")
    order = []
    for _ in range(3):
        while (job := runner.run_once()) is not None:
            order.append((job.tenant, job.id))
        clock.advance(1)
    ticks = [job.id for job in runner.list() if job.definition_id == definition_id]
    assert order == [("web", web[0].id), ("cron", ticks[0]), ("web", web[1].id),
                     ("web", web[2].id), ("cron", ticks[1]), ("cron", ticks[2])]


def test_mixed_workload_statistics_match_the_dashboard(make_runner, clock, jr):
    runner = make_runner({"ok": Script(), "flaky": Script(jr.errors.TransientError("busy")),
                          "bad": Script(ValueError("no"))})
    for kind in ("ok", "flaky", "bad", "ok"):
        post(jr, runner, "/jobs", {"kind": kind})
    cancelled = post(jr, runner, "/jobs", {"kind": "ok", "run_at": clock.now + 50})
    post(jr, runner, f"/jobs/{cancelled['id']}/cancel")
    run_all(runner)
    clock.advance(1)
    run_all(runner)
    stats = get(jr, runner, "/stats")
    assert stats["by_status"] == {"pending": 0, "running": 0, "succeeded": 3, "failed": 1,
                                  "cancelled": 1}
    assert stats["average_attempts"] == pytest.approx(5 / 4)
    html = jr.dashboard.render_page(runner, "/stats")[1]
    assert table_rows(html, "stats-summary") == [["Finished", "4"], ["Failed", "1"],
                                                 ["Failure rate", "25.0%"],
                                                 ["Average attempts", "1.25"]]
    assert table_rows(html, "stats-kind") == [["bad", "1"], ["flaky", "1"], ["ok", "3"]]
