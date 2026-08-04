from __future__ import annotations

import ast
from pathlib import Path

from conftest import WORKSPACE, event_types, queue_request


def test_basic_fifo_lifecycle_remains_available(make_application):
    app = make_application()
    first = app.api.submit(queue_request("first"), request_id="first", now=0)
    second = app.api.submit(queue_request("second"), request_id="second", now=0)
    first_claim = app.api.claim("worker", now=1)
    app.api.complete(first["job_id"], first_claim["lease_token"], "one", now=2)
    second_claim = app.api.claim("worker", now=3)
    app.api.complete(second["job_id"], second_claim["lease_token"], "two", now=4)

    assert first_claim["job_id"] == first["job_id"]
    assert second_claim["job_id"] == second["job_id"]
    assert event_types(app.api) == [
        "submitted", "submitted", "claimed", "completed", "claimed", "completed"
    ]


def test_implementation_is_stdlib_only_and_does_not_reach_grader():
    package = WORKSPACE / "src" / "worker_queue"
    files = sorted(package.glob("*.py"))
    assert len(files) >= 10
    forbidden_roots = {"pytest", "requests", "httpx", "grader_tests", "evals"}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not ({alias.name.split(".")[0] for alias in node.names} & forbidden_roots)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_roots
