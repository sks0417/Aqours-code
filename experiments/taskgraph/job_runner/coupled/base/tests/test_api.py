from jobrunner.api import handle


def test_post_creates_job(runner):
    status, body = handle(runner, "POST", "/jobs", {"kind": "echo", "payload": {"x": 1}})
    assert status == 201
    assert body["kind"] == "echo" and body["status"] == "pending"
    assert body["payload"] == {"x": 1}


def test_post_rejects_bad_body(runner):
    assert handle(runner, "POST", "/jobs", {"payload": {}})[0] == 400
    assert handle(runner, "POST", "/jobs", {"kind": "missing"})[0] == 400


def test_get_job(runner):
    job = runner.submit("echo", {})
    status, body = handle(runner, "GET", f"/jobs/{job.id}")
    assert status == 200 and body["id"] == job.id
    assert handle(runner, "GET", "/jobs/999")[0] == 404


def test_list_jobs_by_status(runner):
    first = runner.submit("echo", {})
    runner.submit("echo", {})
    runner.run_once()
    status, body = handle(runner, "GET", "/jobs?status=succeeded")
    assert status == 200
    assert [job["id"] for job in body["jobs"]] == [first.id]
    assert len(handle(runner, "GET", "/jobs")[1]["jobs"]) == 2
    assert handle(runner, "GET", "/jobs?status=bogus")[0] == 400


def test_unknown_route_is_404(runner):
    assert handle(runner, "DELETE", "/jobs")[0] == 404
    assert handle(runner, "GET", "/nothing")[0] == 404
