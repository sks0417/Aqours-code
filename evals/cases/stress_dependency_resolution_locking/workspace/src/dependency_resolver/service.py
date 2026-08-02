from __future__ import annotations

from .fingerprint import resolution_fingerprint
from .resolver import DependencySolver
from .validation import (
    normalize_features, normalize_lock, normalize_platform,
    normalize_registry, normalize_requirements,
)


class ResolverService:
    def __init__(self, registry, cache):
        self._registry = registry
        self._cache = cache

    def resolve(self, requirements, *, platform, features=None, lock=None):
        normalized_requirements = normalize_requirements(requirements, self._registry)
        normalized_platform = normalize_platform(platform)
        normalized_features = normalize_features(features, self._registry)
        normalized_lock = normalize_lock(lock)
        fingerprint = resolution_fingerprint(
            normalized_requirements, normalized_platform, normalized_features,
            normalized_lock, self._registry.revision)
        cached = self._cache.get(fingerprint)
        if cached is not None:
            return dict(cached)
        result = DependencySolver(self._registry).resolve(
            normalized_requirements, platform=normalized_platform,
            features=normalized_features, lock=normalized_lock)
        self._cache.put(fingerprint, result)
        return dict(result)

    def replace_registry(self, registry):
        self._registry.replace(registry)
        normalized = normalize_registry(registry)
        self._registry.replace(normalized)

    def cache_size(self):
        return self._cache.size()
