from __future__ import annotations

import pytest

from conftest import queue_request
from worker_queue import IdempotencyConflict, ValidationError, build_application


@pytest.mark.parametrize(
    "changed",
    [
        queue_request(payload={"nested": {"enabled": False}}),
        queue_request(payload={"items": [2, 1]}),
        queue_request(max_attempts=4),
        queue_request(task="archive"),
    ],
)
def test_request_id_covers_all_normalized_content(make_application, changed):
    app = make_application()
    app.api.submit(
        queue_request(payload={"nested": {"enabled": True}, "items": [1, 2]}),
        request_id="whole-request", now=0,
    )
    before = app.snapshot()

    with pytest.raises(IdempotencyConflict):
        app.api.submit(changed, request_id="whole-request", now=1)
    assert app.snapshot() == before


def test_mapping_key_order_and_identifier_normalization_are_idempotent(make_application):
    app = make_application()
    first = app.api.submit(
        queue_request("  deliver  ", payload={"z": 1, "a": {"y": 2, "x": 3}}),
        request_id=" key ", now=0,
    )
    second = app.api.submit(
        queue_request("deliver", payload={"a": {"x": 3, "y": 2}, "z": 1}),
        request_id="key", now=100,
    )

    assert second == first
    assert len(app.api.history()) == 1


def test_conflicts_and_validation_do_not_consume_ids_or_append_history(make_application):
    app = make_application()
    first = app.api.submit(queue_request(), request_id="one", now=0)

    with pytest.raises(IdempotencyConflict):
        app.api.submit(queue_request(task="other"), request_id="one", now=1)
    with pytest.raises(ValidationError):
        app.api.submit({"task": "bad", "unknown": True}, request_id="bad", now=1)

    second = app.api.submit(queue_request(), request_id="two", now=2)
    assert first["job_id"] == "job-000001"
    assert second["job_id"] == "job-000002"
    assert [event["sequence"] for event in app.api.history()] == [1, 2]


def test_request_binding_and_nested_payload_survive_restart(make_application):
    app = make_application()
    original = app.api.submit(
        queue_request(payload={"route": ["a", {"b": True}]}),
        request_id="persisted", now=0,
    )
    restarted = build_application(app.snapshot(), lease_seconds=10)
    same = restarted.api.submit(
        queue_request(payload={"route": ["a", {"b": True}]}),
        request_id="persisted", now=50,
    )

    assert same == original
    before = restarted.snapshot()
    with pytest.raises(IdempotencyConflict):
        restarted.api.submit(
            queue_request(payload={"route": ["b"]}), request_id="persisted", now=51
        )
    assert restarted.snapshot() == before
