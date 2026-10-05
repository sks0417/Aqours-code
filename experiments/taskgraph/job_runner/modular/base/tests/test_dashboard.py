from jobrunner.dashboard import render_jobs


def test_dashboard_lists_jobs(runner):
    first = runner.submit("echo", {})
    second = runner.submit("echo", {})
    html = render_jobs([runner.get(first.id), runner.get(second.id)])
    assert html.startswith("<table")
    for column in ("ID", "Kind", "Status"):
        assert f"<th>{column}</th>" in html
    assert html.count("<tr>") == 3
    assert f"<td>{first.id}</td>" in html and "<td>pending</td>" in html


def test_dashboard_escapes_job_text(runner):
    runner.handlers["<b>x</b>"] = lambda payload: None
    job = runner.submit("<b>x</b>", {})
    html = render_jobs([runner.get(job.id)])
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
