from __future__ import annotations

import inspect

import pytest

from conftest import daily, resources
from series_scheduler import ValidationError, build_application


@pytest.mark.parametrize("name", [
    "ValidationError", "UnknownResource", "DuplicateSeries", "UnknownSeries",
    "UnknownOccurrence", "OccurrenceCancelled", "ScheduleConflict",
    "CapacityExceeded", "IdempotencyConflict", "RequestConflict",
])
def test_documented_errors_inherit_base(name):
    import series_scheduler

    assert issubclass(
        getattr(series_scheduler, name), series_scheduler.SchedulerError)


def test_results_are_fresh_json_compatible_copies():
    app = build_application(resources(timezone="UTC"))
    created = app.api.create_series(daily(count=1), idempotency_key="series")
    created["series_id"] = "forged"
    replay = app.api.create_series(daily(count=1), idempotency_key="series")
    assert replay["series_id"] == "clinic"
    values = app.api.occurrences(
        "clinic", "2026-03-01T00:00Z", "2026-04-01T00:00Z")
    values[0]["reserved"] = 99
    assert app.api.occurrences(
        "clinic", "2026-03-01T00:00Z",
        "2026-04-01T00:00Z")[0]["reserved"] == 0


def test_naive_query_instants_and_aware_local_starts_are_rejected():
    app = build_application(resources())
    with pytest.raises(ValidationError):
        app.api.create_series(
            daily(start="2026-03-07T09:00+00:00"), idempotency_key="bad")
    app.api.create_series(daily(count=1), idempotency_key="good")
    with pytest.raises(ValidationError) as caught:
        app.api.occurrences(
            "clinic", "2026-03-01T00:00", "2026-04-01T00:00Z")
    assert caught.value.field == "window_start"


def test_facade_delegates_without_storage_access():
    import series_scheduler.api as module

    source = inspect.getsource(module.SchedulerAPI)
    for marker in ("._values", "._bindings", "._next"):
        assert marker not in source
