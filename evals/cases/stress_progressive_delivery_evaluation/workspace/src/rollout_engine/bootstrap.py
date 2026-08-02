from __future__ import annotations

from dataclasses import dataclass

from .api import RolloutAPI
from .repositories import (
    ConfigurationRepository, ExposureRepository, RequestRepository,
)
from .service import RolloutService
from .validation import normalize_configuration


@dataclass
class RolloutApplication:
    api: RolloutAPI
    configurations: ConfigurationRepository
    requests: RequestRepository
    exposure_repository: ExposureRepository


def build_application(flags, segments=None) -> RolloutApplication:
    normalized_flags, normalized_segments = normalize_configuration(flags, segments)
    configurations = ConfigurationRepository(normalized_flags, normalized_segments)
    requests = RequestRepository()
    exposures = ExposureRepository()
    service = RolloutService(configurations, requests, exposures)
    return RolloutApplication(
        RolloutAPI(service), configurations, requests, exposures)


def build_api(flags, segments=None) -> RolloutAPI:
    return build_application(flags, segments).api
