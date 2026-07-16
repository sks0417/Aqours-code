from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from .api import ReservationAPI
from .idempotency_repository import IdempotencyRepository
from .inventory_repository import InventoryRepository
from .reservation_repository import ReservationRepository
from .service import ReservationService, SequentialReservationId
from .validation import validate_initial_stock


@dataclass(frozen=True, slots=True)
class InventoryApplication:
    api: ReservationAPI
    service: ReservationService
    inventory: InventoryRepository
    reservations: ReservationRepository
    idempotency: IdempotencyRepository


def build_application(
    initial_stock: Mapping[str, int],
    *,
    id_prefix: str = "rsv",
) -> InventoryApplication:
    normalized_stock = validate_initial_stock(initial_stock)
    inventory = InventoryRepository(normalized_stock)
    reservations = ReservationRepository()
    idempotency = IdempotencyRepository()
    service = ReservationService(
        inventory=inventory,
        reservations=reservations,
        idempotency=idempotency,
        id_factory=SequentialReservationId(id_prefix),
    )
    api = ReservationAPI(service)
    return InventoryApplication(
        api=api,
        service=service,
        inventory=inventory,
        reservations=reservations,
        idempotency=idempotency,
    )


def build_api(initial_stock: Mapping[str, int]) -> ReservationAPI:
    return build_application(initial_stock).api
