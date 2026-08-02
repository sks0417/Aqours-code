from __future__ import annotations


class ResolverAPI:
    def __init__(self, service):
        self._service = service

    def resolve(self, requirements, *, platform, features=None, lock=None):
        return self._service.resolve(
            requirements, platform=platform, features=features, lock=lock)

    def replace_registry(self, registry):
        return self._service.replace_registry(registry)

    def cache_size(self):
        return self._service.cache_size()
