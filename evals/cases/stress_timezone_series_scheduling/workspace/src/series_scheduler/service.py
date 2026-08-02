from __future__ import annotations

from datetime import timedelta

from .conflicts import first_conflict, parse_canonical
from .errors import (
    CapacityExceeded, OccurrenceCancelled, ScheduleConflict, UnknownOccurrence,
)
from .fingerprint import booking_fingerprint, series_fingerprint
from .models import Booking
from .recurrence import generated_occurrences
from .serialization import serialize_booking, serialize_occurrence
from .timezones import canonical_utc
from .validation import (
    normalize_key, normalize_seats, normalize_series, normalize_window,
    parse_instant,
)


class SchedulerService:
    def __init__(self, resources, series, occurrences, creations, requests,
                 bookings, booking_ids):
        self._resources = resources
        self._series = series
        self._occurrences = occurrences
        self._creations = creations
        self._requests = requests
        self._bookings = bookings
        self._booking_ids = booking_ids

    def create_series(self, payload, *, idempotency_key):
        key = normalize_key(idempotency_key, "idempotency_key")
        series = normalize_series(payload, self._resources)
        fingerprint = series_fingerprint(series)
        existing = self._creations.resolve(key, fingerprint)
        if existing is not None:
            return dict(existing)
        self._creations.bind(key, fingerprint, None)
        resource = self._resources.get(series.resource_id)
        candidates = generated_occurrences(series, resource)
        conflict = first_conflict(candidates, self._occurrences.all())
        if conflict is not None:
            raise ScheduleConflict(series.resource_id, conflict["start"])
        self._series.add(series)
        self._occurrences.add_many(candidates)
        result = {
            "series_id": series.series_id,
            "occurrence_count": len(candidates),
            "first_start": candidates[0]["start"] if candidates else None,
            "last_start": candidates[-1]["start"] if candidates else None,
        }
        self._creations.bind(key, fingerprint, result)
        return dict(result)

    def occurrences(self, series_id, window_start, window_end):
        self._series.get(series_id)
        left, right = normalize_window(window_start, window_end)
        values = [
            item for item in self._occurrences.all()
            if item["series_id"] == series_id and not item["cancelled"]
            and left <= item["start"] <= right
        ]
        values.sort(key=lambda item: (item["start"], item["original_start"]))
        return [serialize_occurrence(item) for item in values]

    def cancel_occurrence(self, series_id, original_start):
        self._series.get(series_id)
        original = canonical_utc(parse_instant(original_start, "original_start"))
        occurrence = self._occurrences.find_original(series_id, original)
        if occurrence is None:
            raise UnknownOccurrence(series_id, original)
        occurrence["cancelled"] = True
        return serialize_occurrence(occurrence)

    def reschedule_occurrence(self, series_id, original_start, new_start):
        self._series.get(series_id)
        original = canonical_utc(parse_instant(original_start, "original_start"))
        occurrence = self._occurrences.find_original(series_id, original)
        if occurrence is None:
            raise UnknownOccurrence(series_id, original)
        if occurrence["cancelled"]:
            raise OccurrenceCancelled(series_id, original)
        start = canonical_utc(parse_instant(new_start, "new_start"))
        duration = parse_canonical(occurrence["end"]) - parse_canonical(occurrence["start"])
        candidate = dict(occurrence)
        candidate["start"] = start
        candidate["end"] = canonical_utc(parse_canonical(start) + duration)
        conflict = first_conflict(
            [candidate], self._occurrences.all(),
            exclude=(series_id, original))
        if conflict is not None:
            raise ScheduleConflict(occurrence["resource_id"], start)
        occurrence.update({"start": candidate["start"], "end": candidate["end"]})
        return serialize_occurrence(occurrence)

    def book(self, series_id, occurrence_start, seats, *, request_id):
        request = normalize_key(request_id, "request_id")
        normalized_seats = normalize_seats(seats)
        start = canonical_utc(parse_instant(occurrence_start, "occurrence_start"))
        fingerprint = booking_fingerprint(series_id, start, normalized_seats)
        existing = self._requests.resolve(request, fingerprint)
        if existing is not None:
            return serialize_booking(existing)
        self._requests.bind(request, fingerprint, None)
        self._series.get(series_id)
        occurrence = self._occurrences.find_effective(series_id, start)
        if occurrence is None:
            raise UnknownOccurrence(series_id, start)
        if normalized_seats > occurrence["capacity"]:
            raise CapacityExceeded(
                series_id, start, normalized_seats,
                occurrence["capacity"] - occurrence["reserved"])
        booking = Booking(
            self._booking_ids.allocate(), series_id, start, normalized_seats)
        occurrence["reserved"] += normalized_seats
        self._bookings.add(booking)
        self._requests.bind(request, fingerprint, booking)
        return serialize_booking(booking)

    def bookings(self):
        return [serialize_booking(value) for value in self._bookings.all()]
