import re

from conftest import Script

COLUMNS = ("ID", "Kind", "Status", "Attempts", "Next retry", "Actions")


def cancel_form(job_id):
    return f'<form method="post" action="/jobs/{job_id}/cancel">'


def rows(html):
    return re.findall(r"<tr>.*?</tr>", html, flags=re.S)


def test_dashboard_has_status_columns(make_runner, jr):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    html = jr.dashboard.render_jobs([runner.get(job.id)])
    headers = re.findall(r"<th>(.*?)</th>", html)
    assert tuple(headers) == COLUMNS


def test_dashboard_shows_attempts_and_next_retry(make_runner, clock, jr):
    runner = make_runner({"work": Script(jr.errors.TransientError("flaky"))})
    job = runner.submit("work", {})
    runner.run_once()
    html = jr.dashboard.render_jobs([runner.get(job.id)])
    row = rows(html)[-1]
    assert "<td>1/3</td>" in row
    assert f"<td>{clock.now + 1.0:.1f}</td>" in row


def test_pending_and_running_jobs_have_cancel_form(make_runner, jr):
    box = {}

    def render_while_running():
        job = box["runner"].get(box["id"])
        box["html"] = jr.dashboard.render_jobs([job])
        return "ok"

    runner = make_runner({"work": Script(render_while_running)})
    box["runner"] = runner
    job = runner.submit("work", {})
    box["id"] = job.id
    html = jr.dashboard.render_jobs([runner.get(job.id)])
    assert cancel_form(job.id) in html
    runner.run_once()
    assert "<td>running</td>" in box["html"] and cancel_form(job.id) in box["html"]


def test_finished_jobs_have_no_cancel_form(make_runner, jr):
    runner = make_runner({"ok": Script(), "bad": Script(ValueError("x"))})
    done = runner.submit("ok", {})
    failed = runner.submit("bad", {})
    runner.run_once()
    runner.run_once()
    cancelled = runner.submit("ok", {})
    runner.cancel(cancelled.id)
    html = jr.dashboard.render_jobs([runner.get(j.id) for j in (done, failed, cancelled)])
    assert "<form" not in html
    assert "<td>cancelled</td>" in html and "<td>0/3</td>" in html


def test_new_job_shows_no_next_retry(make_runner, jr):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    row = rows(jr.dashboard.render_jobs([runner.get(job.id)]))[-1]
    assert "<td>0/3</td>" in row and "<td>-</td>" in row
