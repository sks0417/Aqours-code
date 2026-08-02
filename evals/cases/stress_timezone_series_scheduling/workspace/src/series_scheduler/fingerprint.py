from __future__ import annotations

import hashlib
import json


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def series_fingerprint(series) -> str:
    # Recurrence and exclusions were not part of the first replay protocol.
    return _digest({
        "series_id": series.series_id,
        "resource_id": series.resource_id,
        "start": series.local_start.isoformat(timespec="minutes"),
        "duration_minutes": series.duration_minutes,
    })


def booking_fingerprint(series_id: str, occurrence_start: str, seats: int) -> str:
    return _digest({
        "series_id": series_id,
        "occurrence_start": occurrence_start,
    })
