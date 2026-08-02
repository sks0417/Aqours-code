from __future__ import annotations

from conftest import daily, resources, weekly
from series_scheduler import build_application


def starts(app, series_id, left="2026-01-01T00:00Z", right="2027-01-01T00:00Z"):
    return [
        value["start"]
        for value in app.api.occurrences(series_id, left, right)
    ]


def test_daily_interval_and_dst_preserve_wall_clock():
    app = build_application(resources())
    app.api.create_series(
        daily(start="2026-03-06T09:00", count=3, interval=2),
        idempotency_key="daily")
    assert starts(app, "clinic") == [
        "2026-03-06T14:00Z",
        "2026-03-08T13:00Z",
        "2026-03-10T13:00Z",
    ]


def test_weekdays_are_calendar_sorted_and_interval_is_week_based():
    app = build_application(resources(timezone="UTC"))
    app.api.create_series(
        weekly(weekdays=["WE", "MO"], interval=2, count=4),
        idempotency_key="weekly")
    assert starts(app, "rounds") == [
        "2026-03-02T09:00Z", "2026-03-04T09:00Z",
        "2026-03-16T09:00Z", "2026-03-18T09:00Z",
    ]


def test_nonexistent_is_omitted_and_ambiguous_uses_fold_zero():
    app = build_application(resources())
    app.api.create_series(
        daily("gap", "2026-03-07T02:30", count=3), idempotency_key="gap")
    assert starts(app, "gap") == [
        "2026-03-07T07:30Z", "2026-03-09T06:30Z"]

    app.api.create_series(
        daily("fold", "2026-11-01T01:30", count=1),
        idempotency_key="fold")
    assert starts(app, "fold") == ["2026-11-01T05:30Z"]


def test_window_is_half_open_at_end():
    app = build_application(resources(timezone="UTC"))
    app.api.create_series(
        daily(start="2026-03-07T09:00", count=2), idempotency_key="window")
    values = app.api.occurrences(
        "clinic", "2026-03-07T09:00Z", "2026-03-08T09:00Z")
    assert [item["start"] for item in values] == ["2026-03-07T09:00Z"]
