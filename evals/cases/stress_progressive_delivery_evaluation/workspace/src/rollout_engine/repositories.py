from __future__ import annotations

from copy import deepcopy

from .errors import RequestConflict


class ConfigurationRepository:
    def __init__(self, flags: dict, segments: dict):
        self._flags = deepcopy(flags)
        self._segments = deepcopy(segments)

    def flag(self, key: str):
        return deepcopy(self._flags.get(key))

    def segment(self, name: str):
        return deepcopy(self._segments.get(name))

    def replace(self, flags: dict, segments: dict):
        self._flags = deepcopy(flags)
        self._segments = deepcopy(segments)

    def snapshot(self):
        return deepcopy((self._flags, self._segments))


class RequestRepository:
    def __init__(self):
        self._bindings = {}

    def resolve(self, request_id: str, fingerprint: str):
        binding = self._bindings.get(request_id)
        if binding is None:
            return None
        stored_fingerprint, result = binding
        if stored_fingerprint != fingerprint:
            raise RequestConflict(request_id)
        return result

    def bind(self, request_id: str, fingerprint: str, result):
        self._bindings[request_id] = (fingerprint, result)

    def snapshot(self):
        return deepcopy(self._bindings)

    def restore(self, snapshot):
        self._bindings = deepcopy(snapshot)


class ExposureRepository:
    def __init__(self):
        self._values = []

    def append_many(self, values):
        self._values.extend(values)

    def all(self):
        return tuple(self._values)

    def snapshot(self):
        return list(self._values)

    def restore(self, snapshot):
        self._values = list(snapshot)
