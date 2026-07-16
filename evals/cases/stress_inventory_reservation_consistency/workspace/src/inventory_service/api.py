from __future__ import annotations

from collections.abc import Mapping

from .serialization import inventory_to_dict, reservation_to_dict
from .service import ReservationService
from .validation import (
    validate_idempotency_key,
    validate_reservation_id,
    validate_reservation_request,
    validate_sku,
)


class ReservationAPI:
    """Validation and serialization facade for reservation operations."""

    def __init__(self, service: ReservationService):
        self._service = service

    def reserve(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        request = validate_reservation_request(payload)
        key = validate_idempotency_key(idempotency_key)
        reservation = self._service.reserve(request, key)
        return reservation_to_dict(reservation)

    def cancel(self, reservation_id: str) -> dict[str, object]:
        normalized_id = validate_reservation_id(reservation_id)
        return reservation_to_dict(self._service.cancel(normalized_id))

    def confirm(self, reservation_id: str) -> dict[str, object]:
        normalized_id = validate_reservation_id(reservation_id)
        return reservation_to_dict(self._service.confirm(normalized_id))

    def get_reservation(self, reservation_id: str) -> dict[str, object]:
        normalized_id = validate_reservation_id(reservation_id)
        return reservation_to_dict(self._service.get(normalized_id))

    def get_inventory(self, sku: str) -> dict[str, object]:
        normalized_sku = validate_sku(sku)
        inventory = self._service.inventory
        return inventory_to_dict(
            normalized_sku,
            inventory.available(normalized_sku),
            inventory.initial(normalized_sku),
        )
