from __future__ import annotations

from copy import deepcopy


class RegistryRepository:
    def __init__(self, registry):
        self._registry = deepcopy(registry)
        self._revision = 1

    def package(self, name):
        return deepcopy(self._registry.get(name))

    def replace(self, registry):
        self._registry = deepcopy(registry)
        self._revision += 1

    def snapshot(self):
        return deepcopy(self._registry), self._revision

    @property
    def revision(self):
        return self._revision


class ResolutionCache:
    def __init__(self):
        self._values = {}

    def get(self, fingerprint):
        return self._values.get(fingerprint)

    def put(self, fingerprint, result):
        self._values[fingerprint] = result

    def clear(self):
        self._values.clear()

    def snapshot(self):
        return deepcopy(self._values)

    def restore(self, snapshot):
        self._values = deepcopy(snapshot)

    def size(self):
        return len(self._values)
