from __future__ import annotations

from collections.abc import Iterable, Mapping

from .errors import (
    InsufficientInventory,
    InventoryInvariantViolation,
    UnknownSku,
)
from .models import ReservationLine


class InventoryRepository:
    """In-memory inventory adapter with checked reserve/release operations."""

    def __init__(self, initial_stock: Mapping[str, int]):
        self._initial = dict(initial_stock)
        self._available = dict(initial_stock)

    def available(self, sku: str) -> int:
        if sku not in self._available:
            raise UnknownSku(sku)
        return self._available[sku]

    def initial(self, sku: str) -> int:
        if sku not in self._initial:
            raise UnknownSku(sku)
        return self._initial[sku]

    def reserve(self, items: Iterable[ReservationLine]) -> None:
        """Deduct each item in repository order."""

        lines = tuple(items)
        for line in lines:
            available = self.available(line.sku)
            if available < line.quantity:
                raise InsufficientInventory(
                    line.sku,
                    requested=line.quantity,
                    available=available,
                )
            self._available[line.sku] = available - line.quantity

    def release(self, items: Iterable[ReservationLine]) -> None:
        """Release a complete reservation without exceeding initial stock."""

        lines = tuple(items)
        resulting: dict[str, int] = {}
        for line in lines:
            current = self.available(line.sku)
            attempted = current + line.quantity
            initial = self.initial(line.sku)
            if attempted > initial:
                raise InventoryInvariantViolation(line.sku, attempted, initial)
            resulting[line.sku] = attempted

        for sku, quantity in resulting.items():
            self._available[sku] = quantity

    def snapshot(self) -> dict[str, int]:
        return dict(sorted(self._available.items()))

    def initial_snapshot(self) -> dict[str, int]:
        return dict(sorted(self._initial.items()))
