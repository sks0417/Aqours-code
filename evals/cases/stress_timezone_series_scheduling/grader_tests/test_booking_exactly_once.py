from __future__ import annotations

import pytest

from conftest import daily, resources, state
from series_scheduler import (
    CapacityExceeded, RequestConflict, UnknownOccurrence, build_application,
)


def prepared(capacity=3):
    app = build_application(resources(capacity, timezone="UTC"))
    app.api.create_series(daily(count=1), idempotency_key="series")
    return app


def test_capacity_is_aggregate_and_failure_is_side_effect_free():
    app = prepared()
    app.api.book("clinic", "2026-03-07T09:00Z", 2, request_id="one")
    before = state(app)
    with pytest.raises(CapacityExceeded) as caught:
        app.api.book("clinic", "2026-03-07T09:00Z", 2, request_id="two")
    assert (caught.value.requested, caught.value.available) == (2, 1)
    assert state(app) == before


def test_seats_are_in_request_fingerprint():
    app = prepared()
    app.api.book("clinic", "2026-03-07T09:00Z", 1, request_id="same")
    before = state(app)
    with pytest.raises(RequestConflict):
        app.api.book("clinic", "2026-03-07T09:00Z", 2, request_id="same")
    assert state(app) == before


def test_unknown_occurrence_does_not_bind_or_consume_id():
    app = prepared()
    before = state(app)
    with pytest.raises(UnknownOccurrence):
        app.api.book(
            "clinic", "2026-03-08T09:00Z", 1, request_id="missing")
    assert state(app) == before


def test_reschedule_preserves_reservations_and_rejects_stale_start():
    app = prepared()
    app.api.book("clinic", "2026-03-07T09:00Z", 2, request_id="before")
    moved = app.api.reschedule_occurrence(
        "clinic", "2026-03-07T09:00Z", "2026-03-07T11:00Z")
    assert moved["reserved"] == 2
    with pytest.raises(UnknownOccurrence):
        app.api.book(
            "clinic", "2026-03-07T09:00Z", 1, request_id="stale")
    booked = app.api.book(
        "clinic", "2026-03-07T11:00Z", 1, request_id="current")
    assert booked["booking_id"] == "booking-000002"
