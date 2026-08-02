from __future__ import annotations

import inspect


def test_exports_and_signatures_are_stable(make_application):
    import series_scheduler

    expected = {
        "SchedulerApplication", "build_api", "build_application",
        "SchedulerError", "ValidationError", "UnknownResource",
        "DuplicateSeries", "UnknownSeries", "UnknownOccurrence",
        "OccurrenceCancelled", "ScheduleConflict", "CapacityExceeded",
        "IdempotencyConflict", "RequestConflict",
    }
    assert expected <= set(series_scheduler.__all__)
    app = make_application()
    assert str(inspect.signature(app.api.create_series)) == (
        "(payload, *, idempotency_key)")
    assert str(inspect.signature(app.api.occurrences)) == (
        "(series_id, window_start, window_end)")
    assert str(inspect.signature(app.api.cancel_occurrence)) == (
        "(series_id, original_start)")
    assert str(inspect.signature(app.api.reschedule_occurrence)) == (
        "(series_id, original_start, new_start)")
    assert str(inspect.signature(app.api.book)) == (
        "(series_id, occurrence_start, seats, *, request_id)")
    assert str(inspect.signature(app.api.bookings)) == "()"


def test_application_exposes_diagnostic_repositories(make_application):
    app = make_application()
    assert app.series.snapshot() == {}
    assert app.occurrence_repository.snapshot() == []
    assert app.creations.snapshot() == {}
    assert app.requests.snapshot() == {}
    assert app.booking_repository.snapshot() == []
    assert app.booking_ids.snapshot() == 1
