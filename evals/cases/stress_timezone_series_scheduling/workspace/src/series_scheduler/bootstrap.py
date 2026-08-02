from __future__ import annotations

from dataclasses import dataclass

from .api import SchedulerAPI
from .repositories import (
    BookingIdSequence, BookingRepository, CreationRepository,
    OccurrenceRepository, RequestRepository, ResourceRepository,
    SeriesRepository,
)
from .service import SchedulerService
from .validation import normalize_resources


@dataclass
class SchedulerApplication:
    api: SchedulerAPI
    resources: ResourceRepository
    series: SeriesRepository
    occurrence_repository: OccurrenceRepository
    creations: CreationRepository
    requests: RequestRepository
    booking_repository: BookingRepository
    booking_ids: BookingIdSequence


def build_application(resources) -> SchedulerApplication:
    resource_repository = ResourceRepository(normalize_resources(resources))
    series = SeriesRepository()
    occurrences = OccurrenceRepository()
    creations = CreationRepository()
    requests = RequestRepository()
    bookings = BookingRepository()
    booking_ids = BookingIdSequence()
    service = SchedulerService(
        resource_repository, series, occurrences, creations, requests,
        bookings, booking_ids)
    return SchedulerApplication(
        SchedulerAPI(service), resource_repository, series, occurrences,
        creations, requests, bookings, booking_ids)


def build_api(resources) -> SchedulerAPI:
    return build_application(resources).api
