from __future__ import annotations

from enum import Enum

from .errors import InvalidStateTransition


class ReservationStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELED = "canceled"


_ALLOWED_TARGETS = {
    ReservationStatus.PENDING: {
        ReservationStatus.CONFIRMED,
        ReservationStatus.CANCELED,
    },
    ReservationStatus.CONFIRMED: set(),
    # A canceled reservation is terminal. This table is consumed by the
    # domain model so transition policy stays in one place.
    ReservationStatus.CANCELED: {ReservationStatus.CONFIRMED},
}


def transition_status(
    reservation_id: str,
    current: ReservationStatus,
    target: ReservationStatus,
) -> ReservationStatus:
    """Validate and return the next status without mutating a reservation."""

    if current is target:
        return current
    if target not in _ALLOWED_TARGETS[current]:
        raise InvalidStateTransition(
            reservation_id,
            current.value,
            target.value,
        )
    return target
