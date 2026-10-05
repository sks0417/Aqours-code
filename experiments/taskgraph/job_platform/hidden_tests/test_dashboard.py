import re

from conftest import Recorder, Script, table_headers, table_rows


def page(jr, runner, path):
    return jr.dashboard.render_page(runner, path)


def title(html):
    match = re.search(r"<h1>(.*?)</h1>", html)
    return match.group(1) if match else None


def test_jobs_page_and_unknown_pages(make_runner, jr):
    runner = make_runner({"work": Script()})
    runner.submit("work", {})
    assert page(jr, runner, "/jobs") == (200, jr.dashboard.render_jobs(runner.list()))
    for path in ("/nothing", "/jobs/999/history", "/jobs/999/dependencies"):
        status, html = page(jr, runner, path)
        assert status == 404 and "Not found" in html, path


def test_queue_page(make_runner, clock, jr):
    runner = make_runner({"work": Script()})
    low = runner.submit("work", {}, tenant="acme")
    high = runner.submit("work", {}, priority=3, run_at=clock.now + 2.5)
    status, html = page(jr, runner, "/queue")
    assert status == 200 and title(html) == "Queue"
    assert table_headers(html, "queue") == ["ID", "Kind", "Tenant", "Priority", "Run at"]
    assert table_rows(html, "queue") == [[str(high.id), "work", "default", "3", "1002.5"],
                                         [str(low.id), "work", "acme", "0", "1000.0"]]
    assert table_headers(html, "tenants") == ["Tenant", "Pending", "Running", "Finished"]
    assert table_rows(html, "tenants") == [["acme", "1", "0", "0"], ["default", "1", "0", "0"]]


def test_recurring_page(make_runner, jr):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {}, 90)
    status, html = page(jr, runner, "/recurring")
    assert status == 200 and title(html) == "Recurring jobs"
    assert table_headers(html, "recurring") == ["ID", "Kind", "Interval", "Next run", "State",
                                                "Last job", "Actions"]
    pause = (f'<form method="post" action="/recurring/{definition_id}/pause">'
             '<button type="submit">Pause</button></form>')
    assert table_rows(html, "recurring") == [[str(definition_id), "tick", "90.0", "1000.0",
                                              "active", "-", pause]]
    job = runner.run_once()
    runner.pause_recurring(definition_id)
    row = table_rows(page(jr, runner, "/recurring")[1], "recurring")[0]
    assert row[3:6] == ["1090.0", "paused", str(job.id)]
    assert row[6] == pause.replace("pause", "resume").replace("Pause", "Resume")


def test_dependencies_page(make_runner, jr):
    runner = make_runner({"work": Script()})
    a = runner.submit("work", {})
    b = runner.submit("work", {}, depends_on=[a.id])
    c = runner.submit("work", {}, depends_on=[b.id])
    runner.run_once()
    status, html = page(jr, runner, f"/jobs/{b.id}/dependencies")
    assert status == 200 and title(html) == f"Job {b.id} dependencies"
    assert table_headers(html, "dependencies") == ["Relation", "ID", "Kind", "Status"]
    assert table_rows(html, "dependencies") == [["depends on", str(a.id), "work", "succeeded"],
                                                ["dependent", str(c.id), "work", "pending"]]


def test_rate_limits_page(make_runner, jr):
    runner = make_runner({"mail": Script(), "fax": Script()})
    runner.set_rate_limit("mail", 5, 60)
    runner.set_rate_limit("fax", 1, 2.5)
    runner.submit("mail", {})
    runner.run_once()
    status, html = page(jr, runner, "/rate-limits")
    assert status == 200 and title(html) == "Rate limits"
    assert table_headers(html, "rate-limits") == ["Kind", "Max starts", "Window",
                                                  "Recent starts"]
    assert table_rows(html, "rate-limits") == [["fax", "1", "2.5", "0"],
                                               ["mail", "5", "60.0", "1"]]


def test_notifications_page(make_runner, clock, jr):
    runner = make_runner({"work": Script()})
    everything = runner.subscribe("https://all.example")
    failures = runner.subscribe("https://fail.example", kinds=["work", "other"],
                                statuses=["failed", "cancelled"])
    job = runner.submit("work", {})
    runner.deliver_notifications(Recorder(fail_urls={"https://all.example": 1}))
    status, html = page(jr, runner, "/notifications")
    assert status == 200 and title(html) == "Notifications"
    assert table_headers(html, "subscriptions") == ["ID", "URL", "Kinds", "Statuses"]
    assert table_rows(html, "subscriptions") == [
        [str(everything.id), "https://all.example", "all", "all"],
        [str(failures.id), "https://fail.example", "work, other", "failed, cancelled"]]
    assert table_headers(html, "outbox") == ["ID", "Subscription", "Job", "Event", "Status",
                                             "Attempts", "Next attempt", "Actions"]
    note = runner.list_notifications()[0]
    assert table_rows(html, "outbox") == [[str(note.id), str(everything.id), str(job.id),
                                           "pending", "pending", "1", "1001.0", ""]]
    clock.advance(1)
    runner.deliver_notifications(Recorder())
    row = table_rows(page(jr, runner, "/notifications")[1], "outbox")[0]
    assert row[4:] == ["delivered", "2", "-", ""]
    runner.subscribe("https://down.example")
    runner.run_once()
    for wait in (1, 2, 4, 8, 16):
        runner.deliver_notifications(Recorder(fail_urls={"https://down.example": 99}))
        clock.advance(wait)
    dead = runner.list_notifications(status="dead")[0]
    retry = (f'<form method="post" action="/notifications/{dead.id}/retry">'
             '<button type="submit">Retry</button></form>')
    rows = table_rows(page(jr, runner, "/notifications")[1], "outbox")
    assert [row[-1] for row in rows if row[0] == str(dead.id)] == [retry]


def test_history_page(make_runner, clock, jr):
    runner = make_runner({"work": Script(ValueError("bad <input>"))})
    job = runner.submit("work", {})
    clock.advance(1.5)
    runner.run_once()
    status, html = page(jr, runner, f"/jobs/{job.id}/history")
    assert status == 200 and title(html) == f"Job {job.id} history"
    assert table_headers(html, "history") == ["Time", "From", "To", "Reason"]
    assert table_rows(html, "history") == [["1000.0", "-", "pending", "submitted"],
                                           ["1001.5", "pending", "running", "started"],
                                           ["1001.5", "running", "failed",
                                            "bad &lt;input&gt;"]]


def test_stats_page(make_runner, clock, jr):
    runner = make_runner({"ok": Script(), "bad": Script(ValueError("x"))})
    for kind in ("ok", "ok", "ok", "bad", "ok"):
        runner.submit(kind, {})
    for _ in range(4):
        runner.run_once()
    status, html = page(jr, runner, "/stats")
    assert status == 200 and title(html) == "Statistics"
    assert table_headers(html, "stats-status") == ["Status", "Count"]
    assert table_rows(html, "stats-status") == [["pending", "1"], ["running", "0"],
                                                ["succeeded", "3"], ["failed", "1"],
                                                ["cancelled", "0"]]
    assert table_headers(html, "stats-kind") == ["Kind", "Count"]
    assert table_rows(html, "stats-kind") == [["bad", "1"], ["ok", "4"]]
    assert table_headers(html, "stats-tenant") == ["Tenant", "Count"]
    assert table_rows(html, "stats-tenant") == [["default", "5"]]
    assert table_headers(html, "stats-summary") == ["Metric", "Value"]
    assert table_rows(html, "stats-summary") == [["Finished", "4"], ["Failed", "1"],
                                                 ["Failure rate", "25.0%"],
                                                 ["Average attempts", "1.00"]]
    clock.advance(10)
    windowed = page(jr, runner, "/stats?window_s=5")
    assert windowed[0] == 200
    assert table_rows(windowed[1], "stats-summary")[0] == ["Finished", "0"]
    assert page(jr, runner, "/stats?window_s=zero")[0] == 400


def test_pages_escape_text(make_runner, jr):
    runner = make_runner({"<b>k</b>": Script()})
    runner.submit("<b>k</b>", {}, tenant="<i>t</i>")
    runner.schedule_recurring("<b>k</b>", {}, 10)
    runner.subscribe("https://x.example/?a=<s>")
    for path in ("/queue", "/recurring", "/notifications"):
        html = page(jr, runner, path)[1]
        assert "<b>k</b>" not in html and "<i>t</i>" not in html and "<s>" not in html, path
    assert "&lt;i&gt;t&lt;/i&gt;" in page(jr, runner, "/queue")[1]
    assert "&lt;b&gt;k&lt;/b&gt;" in page(jr, runner, "/recurring")[1]
    assert "&lt;s&gt;" in page(jr, runner, "/notifications")[1]


def test_audit_page(make_runner, clock, jr):
    runner = make_runner({"work": Script()})
    first = runner.submit("work", {})
    clock.advance(2)
    runner.run_once()
    status, html = page(jr, runner, "/audit")
    assert status == 200 and title(html) == "Audit log"
    assert table_headers(html, "audit") == ["Time", "Job", "From", "To", "Reason"]
    assert table_rows(html, "audit") == [
        ["1000.0", str(first.id), "-", "pending", "submitted"],
        ["1002.0", str(first.id), "pending", "running", "started"],
        ["1002.0", str(first.id), "running", "succeeded", "succeeded"]]
    recent = page(jr, runner, "/audit?since=1000")[1]
    assert [row[3] for row in table_rows(recent, "audit")] == ["running", "succeeded"]
    assert page(jr, runner, "/audit?since=never")[0] == 400
