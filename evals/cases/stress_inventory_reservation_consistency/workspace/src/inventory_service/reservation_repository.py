from __future__ import annotations

from .errors import ReservationNotFound
from .models import Reservation


class ReservationRepository:
    """Repository boundary for reservation aggregate persistence."""

    def __init__(self):
        self._records: dict[str, Reservation] = {}

    def add(self, reservation: Reservation) -> None:
        if reservation.reservation_id in self._records:
            raise ValueError(
                f"duplicate reservation ID: {reservation.reservation_id}"
            )
        self._records[reservation.reservation_id] = reservation

    def save(self, reservation: Reservation) -> None:
        if reservation.reservation_id not in self._records:
            raise ReservationNotFound(reservation.reservation_id)
        self._records[reservation.reservation_id] = reservation

    def get(self, reservation_id: str) -> Reservation | None:
        return self._records.get(reservation_id)

    def require(self, reservation_id: str) -> Reservation:
        reservation = self.get(reservation_id)
        if reservation is None:
            raise ReservationNotFound(reservation_id)
        return reservation

    def count(self) -> int:
        return len(self._records)

    def all(self) -> tuple[Reservation, ...]:
        return tuple(self._records[key] for key in sorted(self._records))
