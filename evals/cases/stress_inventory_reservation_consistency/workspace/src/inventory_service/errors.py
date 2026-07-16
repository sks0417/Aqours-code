from __future__ import annotations


class InventoryServiceError(Exception):
    """Base class for errors intentionally exposed by the service."""


class ValidationError(InventoryServiceError):
    def __init__(self, message: str, *, field: str | None = None):
        self.field = field
        prefix = f"{field}: " if field else ""
        super().__init__(prefix + message)


class UnknownSku(InventoryServiceError):
    def __init__(self, sku: str):
        self.sku = sku
        super().__init__(f"unknown inventory SKU: {sku}")


class InsufficientInventory(InventoryServiceError):
    def __init__(self, sku: str, requested: int, available: int):
        self.sku = sku
        self.requested = requested
        self.available = available
        super().__init__(
            f"insufficient inventory for {sku}: "
            f"requested {requested}, available {available}"
        )


class ReservationNotFound(InventoryServiceError):
    def __init__(self, reservation_id: str):
        self.reservation_id = reservation_id
        super().__init__(f"reservation not found: {reservation_id}")


class IdempotencyConflict(InventoryServiceError):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"idempotency key was already used for another request: {key}")


class InvalidStateTransition(InventoryServiceError):
    def __init__(
        self,
        reservation_id: str,
        current: str,
        target: str,
    ):
        self.reservation_id = reservation_id
        self.current = current
        self.target = target
        super().__init__(
            f"reservation {reservation_id} cannot transition "
            f"from {current} to {target}"
        )


class InventoryInvariantViolation(InventoryServiceError):
    def __init__(self, sku: str, attempted: int, initial: int):
        self.sku = sku
        self.attempted = attempted
        self.initial = initial
        super().__init__(
            f"inventory for {sku} would become {attempted}, "
            f"above initial quantity {initial}"
        )
