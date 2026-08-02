from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Series:
    series_id: str
    resource_id: str
    local_start: datetime
    duration_minutes: int
    recurrence: dict
    exdates: tuple[datetime, ...]


@dataclass(frozen=True)
class Booking:
    booking_id: str
    series_id: str
    occurrence_start: str
    seats: int
