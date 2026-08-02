from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


def resources(capacity=3, timezone="America/New_York"):
    return {"room-a": {"timezone": timezone, "capacity": capacity}}


def daily(series_id="clinic", start="2026-03-07T09:00", *, count=3,
          interval=1, duration=30, resource_id="room-a", exdates=None):
    return {
        "series_id": series_id, "resource_id": resource_id, "start": start,
        "duration_minutes": duration,
        "recurrence": {
            "frequency": "DAILY", "interval": interval, "count": count,
        },
        "exdates": exdates or [],
    }


def weekly(series_id="rounds", start="2026-03-02T09:00", *, count=4,
           interval=1, weekdays=None, duration=30):
    return {
        "series_id": series_id, "resource_id": "room-a", "start": start,
        "duration_minutes": duration,
        "recurrence": {
            "frequency": "WEEKLY", "interval": interval, "count": count,
            "weekdays": weekdays or ["MO", "WE"],
        },
    }


@pytest.fixture
def make_application():
    from series_scheduler import build_application

    return lambda capacity=3: build_application(resources(capacity))


def state(app):
    return {
        "series": app.series.snapshot(),
        "occurrences": app.occurrence_repository.snapshot(),
        "creations": app.creations.snapshot(),
        "requests": app.requests.snapshot(),
        "bookings": app.booking_repository.snapshot(),
        "next_booking": app.booking_ids.snapshot(),
    }
