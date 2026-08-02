from __future__ import annotations

import pytest

from conftest import daily, resources, state
from series_scheduler import (
    DuplicateSeries, IdempotencyConflict, ScheduleConflict, ValidationError,
    build_application,
)


def test_recurrence_and_exdates_are_in_creation_fingerprint():
    app = build_application(resources(timezone="UTC"))
    app.api.create_series(daily(count=1), idempotency_key="same")
    before = state(app)
    with pytest.raises(IdempotencyConflict):
        app.api.create_series(daily(count=2), idempotency_key="same")
    assert state(app) == before


def test_failed_conflict_binds_nothing_and_can_be_retried():
    app = build_application(resources(timezone="UTC"))
    app.api.create_series(
        daily("first", "2026-04-01T09:00", count=1),
        idempotency_key="first")
    conflicting = daily("second", "2026-04-01T09:15", count=1)
    before = state(app)
    with pytest.raises(ScheduleConflict):
        app.api.create_series(conflicting, idempotency_key="second")
    assert state(app) == before
    conflicting["start"] = "2026-04-01T10:00"
    created = app.api.create_series(conflicting, idempotency_key="second")
    assert created["series_id"] == "second"


def test_duplicate_series_under_other_key_is_atomic():
    app = build_application(resources(timezone="UTC"))
    app.api.create_series(daily(count=1), idempotency_key="one")
    before = state(app)
    with pytest.raises(DuplicateSeries):
        app.api.create_series(daily(count=1), idempotency_key="two")
    assert state(app) == before


@pytest.mark.parametrize("field,value", [
    ("duration_minutes", True),
    ("duration_minutes", 0),
])
def test_numeric_validation_rejects_bool_and_zero(field, value):
    app = build_application(resources())
    payload = daily()
    payload[field] = value
    with pytest.raises(ValidationError) as caught:
        app.api.create_series(payload, idempotency_key="invalid")
    assert caught.value.field == field
