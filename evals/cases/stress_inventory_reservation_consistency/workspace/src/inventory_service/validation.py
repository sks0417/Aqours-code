from __future__ import annotations

import re
from collections.abc import Mapping

from .errors import ValidationError
from .models import ReservationLine, ReservationRequest


_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("must be a string", field=field)
    normalized = value.strip()
    if not normalized:
        raise ValidationError("must not be empty", field=field)
    return normalized


def validate_idempotency_key(value: object) -> str:
    key = _required_text(value, "idempotency_key")
    if len(key) > 128:
        raise ValidationError("must be at most 128 characters", field="idempotency_key")
    if not _KEY_PATTERN.fullmatch(key):
        raise ValidationError(
            "contains unsupported characters",
            field="idempotency_key",
        )
    return key


def validate_reservation_id(value: object) -> str:
    return _required_text(value, "reservation_id")


def validate_sku(value: object, *, field: str = "sku") -> str:
    return _required_text(value, field)


def validate_reservation_request(payload: object) -> ReservationRequest:
    if not isinstance(payload, Mapping):
        raise ValidationError("must be a mapping", field="payload")

    order_id = _required_text(payload.get("order_id"), "order_id")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValidationError("must be a list", field="items")
    if not raw_items:
        raise ValidationError("must not be empty", field="items")

    combined: dict[str, int] = {}
    for index, raw_item in enumerate(raw_items):
        item_field = f"items[{index}]"
        if not isinstance(raw_item, Mapping):
            raise ValidationError("must be a mapping", field=item_field)
        sku = validate_sku(raw_item.get("sku"), field=f"{item_field}.sku")
        quantity = raw_item.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValidationError(
                "must be an integer",
                field=f"{item_field}.quantity",
            )
        if quantity <= 0:
            raise ValidationError(
                "must be greater than zero",
                field=f"{item_field}.quantity",
            )
        combined[sku] = combined.get(sku, 0) + quantity

    items = tuple(
        ReservationLine(sku=sku, quantity=quantity)
        for sku, quantity in sorted(combined.items())
    )
    return ReservationRequest(order_id=order_id, items=items)


def validate_initial_stock(initial_stock: object) -> dict[str, int]:
    if not isinstance(initial_stock, Mapping):
        raise ValidationError("must be a mapping", field="initial_stock")
    if not initial_stock:
        raise ValidationError("must not be empty", field="initial_stock")

    normalized: dict[str, int] = {}
    for raw_sku, quantity in initial_stock.items():
        sku = validate_sku(raw_sku, field="initial_stock.sku")
        if sku in normalized:
            raise ValidationError("contains duplicate normalized SKU", field="initial_stock")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValidationError("quantity must be an integer", field=f"initial_stock.{sku}")
        if quantity < 0:
            raise ValidationError(
                "quantity must be non-negative",
                field=f"initial_stock.{sku}",
            )
        normalized[sku] = quantity
    return normalized
