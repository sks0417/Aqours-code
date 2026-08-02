from __future__ import annotations

from dataclasses import dataclass

from .api import ResolverAPI
from .repositories import RegistryRepository, ResolutionCache
from .service import ResolverService
from .validation import normalize_registry


@dataclass
class ResolverApplication:
    api: ResolverAPI
    registry: RegistryRepository
    cache: ResolutionCache


def build_application(registry) -> ResolverApplication:
    repository = RegistryRepository(normalize_registry(registry))
    cache = ResolutionCache()
    service = ResolverService(repository, cache)
    return ResolverApplication(ResolverAPI(service), repository, cache)


def build_api(registry) -> ResolverAPI:
    return build_application(registry).api
