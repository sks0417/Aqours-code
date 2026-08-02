from __future__ import annotations


class SchedulerAPI:
    def __init__(self, service):
        self._service = service

    def create_series(self, payload, *, idempotency_key):
        return self._service.create_series(payload, idempotency_key=idempotency_key)

    def occurrences(self, series_id, window_start, window_end):
        return self._service.occurrences(series_id, window_start, window_end)

    def cancel_occurrence(self, series_id, original_start):
        return self._service.cancel_occurrence(series_id, original_start)

    def reschedule_occurrence(self, series_id, original_start, new_start):
        return self._service.reschedule_occurrence(series_id, original_start, new_start)

    def book(self, series_id, occurrence_start, seats, *, request_id):
        return self._service.book(
            series_id, occurrence_start, seats, request_id=request_id)

    def bookings(self):
        return self._service.bookings()
