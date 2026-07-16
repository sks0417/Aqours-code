from __future__ import annotations

import hashlib
import json

from .models import Reservation, ReservationRequest


def request_fingerprint(request: ReservationRequest) -> str:
    """Return a stable digest for a normalized reservation request."""

    canonical = {
        "order_id": request.order_id,
        "items": [
            {"sku": item.sku}
            for item in request.items
        ],
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reservation_to_dict(reservation: Reservation) -> dict[str, object]:
    return {
        "reservation_id": reservation.reservation_id,
        "order_id": reservation.order_id,
        "status": reservation.status.value,
        "items": [
            {"sku": line.sku, "quantity": line.quantity}
            for line in reservation.items
        ],
    }


def inventory_to_dict(sku: str, available: int, initial: int) -> dict[str, object]:
    return {
        "sku": sku,
        "available": available,
        "initial": initial,
    }
