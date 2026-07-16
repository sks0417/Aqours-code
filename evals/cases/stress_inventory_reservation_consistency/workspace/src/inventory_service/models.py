from __future__ import annotations

from dataclasses import dataclass

from .state import ReservationStatus, transition_status


@dataclass(frozen=True, slots=True)
class ReservationLine:
    sku: str
    quantity: int


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    order_id: str
    items: tuple[ReservationLine, ...]

    @property
    def total_units(self) -> int:
        return sum(item.quantity for item in self.items)


@dataclass(slots=True)
class Reservation:
    reservation_id: str
    order_id: str
    items: tuple[ReservationLine, ...]
    idempotency_key: str
    status: ReservationStatus = ReservationStatus.PENDING

    def confirm(self) -> ReservationStatus:
        self.status = transition_status(
            self.reservation_id,
            self.status,
            ReservationStatus.CONFIRMED,
        )
        return self.status

    def cancel(self) -> ReservationStatus:
        self.status = transition_status(
            self.reservation_id,
            self.status,
            ReservationStatus.CANCELED,
        )
        return self.status


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    key: str
    request_fingerprint: str
    reservation_id: str
