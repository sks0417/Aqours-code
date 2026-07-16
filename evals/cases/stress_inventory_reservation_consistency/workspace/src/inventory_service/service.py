from __future__ import annotations

from collections.abc import Callable

from .errors import IdempotencyConflict
from .idempotency_repository import IdempotencyRepository
from .inventory_repository import InventoryRepository
from .models import IdempotencyRecord, Reservation, ReservationRequest
from .reservation_repository import ReservationRepository
from .serialization import request_fingerprint


class SequentialReservationId:
    """Deterministic process-local reservation ID source."""

    def __init__(self, prefix: str = "rsv"):
        self._prefix = prefix
        self._next = 1

    def __call__(self) -> str:
        value = f"{self._prefix}-{self._next:06d}"
        self._next += 1
        return value


class ReservationService:
    def __init__(
        self,
        inventory: InventoryRepository,
        reservations: ReservationRepository,
        idempotency: IdempotencyRepository,
        id_factory: Callable[[], str] | None = None,
    ):
        self.inventory = inventory
        self.reservations = reservations
        self.idempotency = idempotency
        self._id_factory = id_factory or SequentialReservationId()

    def reserve(
        self,
        request: ReservationRequest,
        idempotency_key: str,
    ) -> Reservation:
        fingerprint = request_fingerprint(request)
        existing = self.idempotency.get(idempotency_key)
        if existing is not None:
            return self.reservations.require(existing.reservation_id)

        self.inventory.reserve(request.items)
        reservation = Reservation(
            reservation_id=self._id_factory(),
            order_id=request.order_id,
            items=request.items,
            idempotency_key=idempotency_key,
        )
        try:
            self.reservations.add(reservation)
            self.idempotency.add(IdempotencyRecord(
                key=idempotency_key,
                request_fingerprint=fingerprint,
                reservation_id=reservation.reservation_id,
            ))
        except Exception:
            # Repository failures are not expected from the in-memory adapters,
            # but inventory must not remain deducted if persistence rejects the
            # reservation.
            self.inventory.release(request.items)
            raise
        return reservation

    def get(self, reservation_id: str) -> Reservation:
        return self.reservations.require(reservation_id)

    def confirm(self, reservation_id: str) -> Reservation:
        reservation = self.reservations.require(reservation_id)
        reservation.confirm()
        self.reservations.save(reservation)
        return reservation

    def cancel(self, reservation_id: str) -> Reservation:
        reservation = self.reservations.require(reservation_id)
        reservation.cancel()
        self.inventory.release(reservation.items)
        self.reservations.save(reservation)
        return reservation
