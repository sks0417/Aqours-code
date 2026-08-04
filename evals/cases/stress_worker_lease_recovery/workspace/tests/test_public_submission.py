from __future__ import annotations

import pytest

from conftest import request
from worker_queue import IdempotencyConflict


def test_equivalent_submission_returns_original_without_history(app):
    first = app.api.submit(
        request("  deliver  ", payload={"b": 2, "a": [1, 3]}),
        request_id=" req-1 ", now=1,
    )
    second = app.api.submit(
        request("deliver", payload={"a": [1, 3], "b": 2}),
        request_id="req-1", now=99,
    )

    assert second == first
    assert [event["event_type"] for event in app.api.history()] == ["submitted"]
    assert len(app.api.list_jobs()) == 1


def test_same_request_id_with_different_payload_conflicts(app):
    app.api.submit(request(payload={"region": "east"}), request_id="same", now=1)
    before = app.snapshot()

    with pytest.raises(IdempotencyConflict):
        app.api.submit(request(payload={"region": "west"}), request_id="same", now=2)

    assert app.snapshot() == before


def test_payload_and_return_values_are_defensive_copies(app):
    payload = {"items": ["a"]}
    submitted = app.api.submit(request(payload=payload), request_id="copy", now=1)
    payload["items"].append("caller")
    submitted["payload"]["items"].append("returned")

    assert app.api.get_job(submitted["job_id"])["payload"] == {"items": ["a"]}
