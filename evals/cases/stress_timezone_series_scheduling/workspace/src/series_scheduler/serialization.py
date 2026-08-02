from __future__ import annotations

from copy import deepcopy


def serialize_occurrence(value: dict) -> dict:
    return deepcopy({
        key: value[key] for key in (
            "series_id", "resource_id", "original_start", "start", "end",
            "capacity", "reserved")
    })


def serialize_booking(value) -> dict:
    return {
        "booking_id": value.booking_id,
        "series_id": value.series_id,
        "occurrence_start": value.occurrence_start,
        "seats": value.seats,
    }
