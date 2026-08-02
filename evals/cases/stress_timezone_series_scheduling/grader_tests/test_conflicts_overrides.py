from __future__ import annotations

import pytest

from conftest import daily, resources, state
from series_scheduler import (
    OccurrenceCancelled, ScheduleConflict, UnknownOccurrence,
    build_application,
)


def test_touching_occurrences_do_not_conflict_but_overlap_does():
    app = build_application(resources(timezone="UTC"))
    app.api.create_series(
        daily("first", "2026-04-01T09:00", count=1, duration=30),
        idempotency_key="first")
    app.api.create_series(
        daily("adjacent", "2026-04-01T09:30", count=1, duration=30),
        idempotency_key="adjacent")
    before = state(app)
    with pytest.raises(ScheduleConflict):
        app.api.create_series(
            daily("overlap", "2026-04-01T09:15", count=1, duration=10),
            idempotency_key="overlap")
    assert state(app) == before


def test_reschedule_checks_other_series_and_preserves_original_identity():
    app = build_application(resources(timezone="UTC"))
    app.api.create_series(
        daily("a", "2026-04-01T09:00", count=1), idempotency_key="a")
    app.api.create_series(
        daily("b", "2026-04-01T10:00", count=1), idempotency_key="b")
    before = state(app)
    with pytest.raises(ScheduleConflict):
        app.api.reschedule_occurrence(
            "a", "2026-04-01T09:00Z", "2026-04-01T10:00Z")
    assert state(app) == before
    moved = app.api.reschedule_occurrence(
        "a", "2026-04-01T09:00Z", "2026-04-01T11:00+00:00")
    assert moved["original_start"] == "2026-04-01T09:00Z"
    assert moved["start"] == "2026-04-01T11:00Z"


def test_cancel_is_idempotent_and_reschedule_cancelled_fails():
    app = build_application(resources(timezone="UTC"))
    app.api.create_series(daily(count=1), idempotency_key="one")
    first = app.api.cancel_occurrence("clinic", "2026-03-07T09:00Z")
    second = app.api.cancel_occurrence("clinic", "2026-03-07T09:00Z")
    assert first == second
    with pytest.raises(OccurrenceCancelled):
        app.api.reschedule_occurrence(
            "clinic", "2026-03-07T09:00Z", "2026-03-07T10:00Z")
    with pytest.raises(UnknownOccurrence):
        app.api.cancel_occurrence("clinic", "2026-03-08T09:00Z")
