import pytest

from conftest import Crash, Script


def post(jr, runner, path, body=None):
    return jr.api.handle(runner, "POST", path, body)


def test_flaky_job_succeeds_through_the_api(make_runner, clock, jr):
    runner = make_runner({"report": Script(jr.errors.TransientError("busy"),
                                           jr.errors.TransientError("busy"), "sent")})
    status, created = post(jr, runner, "/jobs", {"kind": "report", "payload": {"n": 1}})
    assert status == 201
    for delay in (1.0, 2.0):
        runner.run_once()
        clock.advance(delay)
    runner.run_once()
    _, body = jr.api.handle(runner, "GET", f"/jobs/{created['id']}")
    assert body["status"] == "succeeded" and body["attempts"] == 3
    assert body["result"] == "sent"
    html = jr.dashboard.render_jobs([runner.get(created["id"])])
    assert "<td>3/3</td>" in html and "<form" not in html


def test_cancel_through_api_stops_retries(make_runner, clock, jr):
    work = Script(jr.errors.TransientError("busy"))
    runner = make_runner({"email": work})
    _, created = post(jr, runner, "/jobs", {"kind": "email", "payload": {}})
    runner.run_once()
    status, body = post(jr, runner, f"/jobs/{created['id']}/cancel")
    assert status == 200 and body["status"] == "cancelled"
    clock.advance(60)
    assert runner.run_once() is None and work.calls == 1
    assert post(jr, runner, f"/jobs/{created['id']}/cancel")[0] == 409


def test_restart_recovers_interrupted_and_waiting_jobs(make_runner, clock, jr):
    runner = make_runner({"email": Script(Crash()),
                          "report": Script(jr.errors.TransientError("busy"))})
    _, waiting = post(jr, runner, "/jobs", {"kind": "report", "payload": {}})
    runner.run_once()  # report fails once and waits until now + 1
    _, crashed = post(jr, runner, "/jobs", {"kind": "email", "payload": {}})
    with pytest.raises(Crash):
        runner.run_once()
    clock.advance(0.5)
    email, report = Script("mailed"), Script("reported")
    restarted = make_runner({"email": email, "report": report})
    _, listed = jr.api.handle(restarted, "GET", "/jobs?status=pending")
    assert [job["id"] for job in listed["jobs"]] == [waiting["id"], crashed["id"]]
    clock.advance(0.5)
    assert restarted.run_once().id == waiting["id"]
    assert restarted.run_once() is None  # the crashed job waits until restart + 1
    clock.advance(0.5)
    assert restarted.run_once().id == crashed["id"]
    for job_id in (waiting["id"], crashed["id"]):
        _, body = jr.api.handle(restarted, "GET", f"/jobs/{job_id}")
        assert body["status"] == "succeeded" and body["attempts"] == 2


def test_mixed_outcomes_are_listed_by_status(make_runner, clock, jr):
    runner = make_runner({"ok": Script("done"), "bad": Script(ValueError("broken")),
                          "slow": Script()})
    ok = runner.submit("ok", {})
    bad = runner.submit("bad", {})
    slow = runner.submit("slow", {})
    runner.run_once()
    runner.run_once()
    runner.cancel(slow.id)
    expected = {"succeeded": [ok.id], "failed": [bad.id], "cancelled": [slow.id],
                "pending": []}
    for status, ids in expected.items():
        _, body = jr.api.handle(runner, "GET", f"/jobs?status={status}")
        assert [job["id"] for job in body["jobs"]] == ids
    _, failed = jr.api.handle(runner, "GET", f"/jobs/{bad.id}")
    assert failed["last_error"] == "broken" and failed["attempts"] == 1
