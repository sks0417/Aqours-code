from __future__ import annotations

from conftest import daily, resources
from series_scheduler import build_application


def test_daily_wall_time_survives_dst_change():
    app = build_application(resources())
    created = app.api.create_series(daily(), idempotency_key="series:one")
    assert created["occurrence_count"] == 3
    values = app.api.occurrences(
        "clinic", "2026-03-07T00:00Z", "2026-03-11T00:00Z")
    assert [item["start"] for item in values] == [
        "2026-03-07T14:00Z",
        "2026-03-08T13:00Z",
        "2026-03-09T13:00Z",
    ]


def test_exdate_suppresses_original_local_candidate():
    app = build_application(resources())
    app.api.create_series(
        daily(exdates=["2026-03-08T09:00"]), idempotency_key="series:exdate")
    values = app.api.occurrences(
        "clinic", "2026-03-07T00:00Z", "2026-03-11T00:00Z")
    assert len(values) == 2
    assert all(item["start"] != "2026-03-08T13:00Z" for item in values)
