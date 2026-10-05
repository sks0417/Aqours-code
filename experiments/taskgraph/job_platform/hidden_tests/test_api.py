from conftest import Recorder, Script

JOB_FIELDS = {"id", "kind", "payload", "status", "result", "created_at", "attempts",
              "max_attempts", "next_run_at", "last_error", "cancel_requested", "priority",
              "tenant", "depends_on", "definition_id", "cancel_reason"}


def call(jr, runner, method, path, body=None):
    return jr.api.handle(runner, method, path, body)


def test_post_job_accepts_the_platform_fields(make_runner, clock, jr):
    runner = make_runner({"work": Script()})
    _, parent = call(jr, runner, "POST", "/jobs", {"kind": "work"})
    status, body = call(jr, runner, "POST", "/jobs", {
        "kind": "work", "payload": {"n": 1}, "priority": 4, "tenant": "acme",
        "run_at": clock.now + 30, "depends_on": [parent["id"]]})
    assert status == 201 and set(body) == JOB_FIELDS
    assert (body["priority"], body["tenant"], body["next_run_at"]) == (4, "acme", 1030.0)
    assert body["depends_on"] == [parent["id"]]
    assert body["definition_id"] is None and body["cancel_reason"] is None
    assert set(parent) == JOB_FIELDS and parent["priority"] == 0


def test_post_job_rejects_invalid_fields(make_runner, jr):
    runner = make_runner({"work": Script()})
    for body in ({"kind": "work", "priority": "high"}, {"kind": "work", "tenant": ""},
                 {"kind": "work", "run_at": "soon"}, {"kind": "work", "depends_on": [999]},
                 {"kind": "work", "depends_on": "1"}, ["work"]):
        status, reply = call(jr, runner, "POST", "/jobs", body)
        assert status == 400 and "error" in reply, body
    assert call(jr, runner, "GET", "/jobs")[1] == {"jobs": []}


def test_priority_and_queue_routes(make_runner, jr):
    runner = make_runner({"work": Script()})
    first = runner.submit("work", {})
    second = runner.submit("work", {})
    status, body = call(jr, runner, "POST", f"/jobs/{second.id}/priority", {"priority": 2})
    assert status == 200 and body["priority"] == 2
    status, body = call(jr, runner, "GET", "/queue")
    assert status == 200 and [job["id"] for job in body["jobs"]] == [second.id, first.id]
    assert call(jr, runner, "POST", f"/jobs/{first.id}/priority", {"priority": "x"})[0] == 400
    assert call(jr, runner, "POST", f"/jobs/{first.id}/priority", {})[0] == 400
    assert call(jr, runner, "POST", "/jobs/999/priority", {"priority": 1})[0] == 404
    runner.run_once()
    status, body = call(jr, runner, "POST", f"/jobs/{second.id}/priority", {"priority": 1})
    assert status == 409 and "error" in body


def test_recurring_routes(make_runner, clock, jr):
    runner = make_runner({"tick": Script()})
    status, created = call(jr, runner, "POST", "/recurring",
                           {"kind": "tick", "interval_s": 60, "tenant": "acme"})
    assert status == 201
    assert (created["kind"], created["payload"], created["interval_s"]) == ("tick", {}, 60)
    assert created["next_run_at"] == clock.now and created["tenant"] == "acme"
    definition_id = created["id"]
    assert call(jr, runner, "GET", "/recurring")[1] == {"recurring": [created]}
    assert call(jr, runner, "GET", f"/recurring/{definition_id}") == (200, created)
    status, patched = call(jr, runner, "PATCH", f"/recurring/{definition_id}",
                           {"interval_s": 120, "payload": {"n": 1}})
    assert status == 200 and (patched["interval_s"], patched["payload"]) == (120, {"n": 1})
    assert call(jr, runner, "PATCH", f"/recurring/{definition_id}", {"interval_s": -1})[0] == 400
    assert call(jr, runner, "PATCH", f"/recurring/{definition_id}", [1])[0] == 400
    assert call(jr, runner, "PATCH", "/recurring/999", {"priority": 1})[0] == 404
    status, paused = call(jr, runner, "POST", f"/recurring/{definition_id}/pause")
    assert status == 200 and paused["paused"] is True
    status, resumed = call(jr, runner, "POST", f"/recurring/{definition_id}/resume")
    assert status == 200 and resumed["paused"] is False
    assert call(jr, runner, "DELETE", f"/recurring/{definition_id}") == (
        200, {"deleted": definition_id})
    assert call(jr, runner, "GET", f"/recurring/{definition_id}")[0] == 404
    assert call(jr, runner, "POST", f"/recurring/{definition_id}/pause")[0] == 404
    assert call(jr, runner, "DELETE", f"/recurring/{definition_id}")[0] == 404
    for body in ({"kind": "tick"}, {"kind": "tick", "interval_s": 0},
                 {"kind": "nope", "interval_s": 5}, {"interval_s": 5}):
        assert call(jr, runner, "POST", "/recurring", body)[0] == 400, body


def test_dependencies_route(make_runner, jr):
    runner = make_runner({"work": Script()})
    a = runner.submit("work", {})
    b = runner.submit("work", {})
    c = runner.submit("work", {}, depends_on=[b.id, a.id])
    d = runner.submit("work", {}, depends_on=[c.id])
    status, body = call(jr, runner, "GET", f"/jobs/{c.id}/dependencies")
    assert status == 200 and body["job_id"] == c.id
    assert [job["id"] for job in body["depends_on"]] == [b.id, a.id]
    assert [job["id"] for job in body["dependents"]] == [d.id]
    assert body["depends_on"][0]["status"] == "pending"
    assert call(jr, runner, "GET", "/jobs/999/dependencies")[0] == 404


def test_rate_limit_routes(make_runner, jr):
    runner = make_runner({"mail": Script()})
    status, body = call(jr, runner, "PUT", "/rate-limits/mail",
                        {"max_starts": 2, "window_s": 30})
    assert status == 200 and body == {"kind": "mail", "max_starts": 2, "window_s": 30,
                                      "recent_starts": 0}
    runner.submit("mail", {})
    runner.run_once()
    status, body = call(jr, runner, "GET", "/rate-limits")
    assert status == 200 and body["rate_limits"][0]["recent_starts"] == 1
    for bad in ({"max_starts": 0, "window_s": 30}, {"window_s": 30}, {"max_starts": 1}):
        assert call(jr, runner, "PUT", "/rate-limits/mail", bad)[0] == 400
    assert call(jr, runner, "PUT", "/rate-limits/nope", {"max_starts": 1, "window_s": 1})[0] == 400
    assert call(jr, runner, "DELETE", "/rate-limits/mail") == (200, {"deleted": "mail"})
    assert call(jr, runner, "DELETE", "/rate-limits/mail")[0] == 404
    assert call(jr, runner, "GET", "/rate-limits")[1] == {"rate_limits": []}


def test_subscription_routes(make_runner, jr):
    runner = make_runner({"work": Script()})
    status, sub = call(jr, runner, "POST", "/subscriptions",
                       {"url": "https://hooks.example", "statuses": ["failed"]})
    assert status == 201 and sub == {"id": sub["id"], "url": "https://hooks.example",
                                     "kinds": None, "statuses": ["failed"]}
    assert call(jr, runner, "GET", "/subscriptions") == (200, {"subscriptions": [sub]})
    for bad in ({}, {"url": ""}, {"url": "https://x", "statuses": ["lost"]},
                {"url": "https://x", "kinds": "work"}):
        assert call(jr, runner, "POST", "/subscriptions", bad)[0] == 400, bad
    assert call(jr, runner, "DELETE", f"/subscriptions/{sub['id']}") == (
        200, {"deleted": sub["id"]})
    assert call(jr, runner, "DELETE", f"/subscriptions/{sub['id']}")[0] == 404


def test_notifications_route(make_runner, jr):
    runner = make_runner({"work": Script()})
    runner.subscribe("https://hooks.example")
    first = runner.submit("work", {})
    second = runner.submit("work", {})
    runner.deliver_notifications(Recorder())
    runner.run_once()
    status, body = call(jr, runner, "GET", "/notifications")
    assert status == 200 and len(body["notifications"]) == 4
    note = body["notifications"][0]
    assert note["payload"]["new_status"] == "pending" and note["status"] == "delivered"
    _, pending = call(jr, runner, "GET", f"/notifications?status=pending&job_id={first.id}")
    assert [n["payload"]["new_status"] for n in pending["notifications"]] == [
        "running", "succeeded"]
    _, only = call(jr, runner, "GET", f"/notifications?job_id={second.id}")
    assert [n["job_id"] for n in only["notifications"]] == [second.id]
    assert call(jr, runner, "GET", "/notifications?status=lost")[0] == 400
    assert call(jr, runner, "GET", "/notifications?job_id=abc")[0] == 400


def test_history_route(make_runner, jr):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    runner.run_once()
    status, body = call(jr, runner, "GET", f"/jobs/{job.id}/history")
    assert status == 200 and body["job_id"] == job.id
    assert body["history"][0] == {"job_id": job.id, "old_status": None,
                                  "new_status": "pending", "time": 1000.0,
                                  "reason": "submitted"}
    assert [e["new_status"] for e in body["history"]] == ["pending", "running", "succeeded"]
    assert call(jr, runner, "GET", "/jobs/999/history")[0] == 404


def test_stats_route(make_runner, clock, jr):
    runner = make_runner({"ok": Script(), "bad": Script(ValueError("x"))})
    runner.submit("bad", {})
    runner.run_once()
    clock.advance(100)
    runner.submit("ok", {})
    runner.run_once()
    status, body = call(jr, runner, "GET", "/stats")
    assert status == 200 and body["window_s"] is None
    assert (body["finished"], body["failure_rate"]) == (2, 0.5)
    assert body["by_kind"] == {"bad": 1, "ok": 1}
    status, body = call(jr, runner, "GET", "/stats?window_s=10")
    assert status == 200 and body["window_s"] == 10 and body["finished"] == 1
    status, body = call(jr, runner, "GET", "/stats?window_s=2.5")
    assert status == 200 and body["window_s"] == 2.5
    for query in ("abc", "0", "-3"):
        assert call(jr, runner, "GET", f"/stats?window_s={query}")[0] == 400


def test_unknown_platform_routes_are_404(make_runner, jr):
    runner = make_runner({"work": Script()})
    for method, path in (("GET", "/recurring/abc"), ("PATCH", "/recurring"),
                         ("DELETE", "/rate-limits"), ("GET", "/subscriptions/1"),
                         ("POST", "/stats"), ("GET", "/jobs/1/audit")):
        status, body = call(jr, runner, method, path)
        assert status == 404 and "error" in body, (method, path)


def test_tenant_recurring_job_audit_and_retry_routes(make_runner, clock, jr):
    runner = make_runner({"work": Script(), "tick": Script()})
    runner.subscribe("https://down.example")
    definition_id = runner.schedule_recurring("tick", {}, 60)
    job = runner.submit("work", {}, tenant="acme")
    runner.run_once()
    status, body = call(jr, runner, "GET", "/tenants")
    assert status == 200 and body == {"tenants": [
        {"tenant": "acme", "pending": 0, "running": 0, "finished": 1},
        {"tenant": "default", "pending": 1, "running": 0, "finished": 0}]}
    status, body = call(jr, runner, "GET", f"/recurring/{definition_id}/jobs")
    assert status == 200 and body["definition_id"] == definition_id
    assert [j["definition_id"] for j in body["jobs"]] == [definition_id]
    assert call(jr, runner, "GET", "/recurring/999/jobs")[0] == 404
    status, body = call(jr, runner, "GET", "/audit")
    assert status == 200 and len(body["entries"]) == 4
    assert body["entries"][0] == {"job_id": job.id, "old_status": None, "new_status": "pending",
                                  "time": 1000.0, "reason": "submitted"}
    clock.advance(5)
    assert call(jr, runner, "GET", "/audit?since=1000")[1] == {"entries": []}
    assert call(jr, runner, "GET", "/audit?since=soon")[0] == 400
    for wait in (1, 2, 4, 8, 16):
        runner.deliver_notifications(Recorder(fail_urls={"https://down.example": 99}))
        clock.advance(wait)
    dead = runner.list_notifications(status="dead")[0]
    status, body = call(jr, runner, "POST", f"/notifications/{dead.id}/retry")
    assert status == 200 and body["status"] == "pending" and body["attempts"] == 0
    assert call(jr, runner, "POST", f"/notifications/{dead.id}/retry")[0] == 409
    assert call(jr, runner, "POST", "/notifications/999/retry")[0] == 404
