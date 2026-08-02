from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def resources(capacity=3):
    return {"room-a": {"timezone": "America/New_York", "capacity": capacity}}


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
