from __future__ import annotations

from copy import deepcopy

from .errors import (
    DuplicateSeries, IdempotencyConflict, RequestConflict, UnknownSeries,
)


class ResourceRepository:
    def __init__(self, resources):
        self._values = deepcopy(resources)

    def get(self, resource_id):
        return deepcopy(self._values.get(resource_id))


class SeriesRepository:
    def __init__(self):
        self._values = {}

    def add(self, series):
        if series.series_id in self._values:
            raise DuplicateSeries(series.series_id)
        self._values[series.series_id] = series

    def get(self, series_id):
        value = self._values.get(series_id)
        if value is None:
            raise UnknownSeries(series_id)
        return value

    def snapshot(self):
        return dict(self._values)

    def restore(self, snapshot):
        self._values = dict(snapshot)


class OccurrenceRepository:
    def __init__(self):
        self._values = []

    def add_many(self, values):
        self._values.extend(deepcopy(values))

    def all(self):
        return deepcopy(self._values)

    def find_original(self, series_id, original_start):
        for value in self._values:
            if (value["series_id"] == series_id
                    and value["original_start"] == original_start):
                return value
        return None

    def find_effective(self, series_id, start):
        for value in self._values:
            if (value["series_id"] == series_id and value["start"] == start
                    and not value["cancelled"]):
                return value
        return None

    def snapshot(self):
        return deepcopy(self._values)

    def restore(self, snapshot):
        self._values = deepcopy(snapshot)


class OperationRepository:
    def __init__(self, conflict_type):
        self._values = {}
        self._conflict_type = conflict_type

    def resolve(self, key, fingerprint):
        binding = self._values.get(key)
        if binding is None:
            return None
        stored, result = binding
        if stored != fingerprint:
            raise self._conflict_type(key)
        return result

    def bind(self, key, fingerprint, result):
        self._values[key] = (fingerprint, result)

    def snapshot(self):
        return deepcopy(self._values)

    def restore(self, snapshot):
        self._values = deepcopy(snapshot)


class CreationRepository(OperationRepository):
    def __init__(self):
        super().__init__(IdempotencyConflict)


class RequestRepository(OperationRepository):
    def __init__(self):
        super().__init__(RequestConflict)


class BookingIdSequence:
    def __init__(self):
        self._next = 1

    def allocate(self):
        value = f"booking-{self._next:06d}"
        self._next += 1
        return value

    def snapshot(self):
        return self._next

    def restore(self, snapshot):
        self._next = int(snapshot)


class BookingRepository:
    def __init__(self):
        self._values = []

    def add(self, value):
        self._values.append(value)

    def all(self):
        return tuple(self._values)

    def snapshot(self):
        return list(self._values)

    def restore(self, snapshot):
        self._values = list(snapshot)
