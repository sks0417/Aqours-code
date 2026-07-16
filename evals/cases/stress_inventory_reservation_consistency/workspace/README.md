# Inventory Reservation Service

This project is a small, in-memory implementation of the reservation boundary
used by an order service. It intentionally keeps persistence adapters separate
from domain and API code so the same business rules can later be used with a
database-backed repository.

Run the public suite from the workspace root:

```console
python -m pytest -q
```

## Public API

`inventory_service.bootstrap.build_application(initial_stock)` creates an
`InventoryApplication`. Its `api` field is the supported facade and its
repository fields are available for diagnostics and adapter tests.

The facade methods and signatures are stable:

```python
api.reserve(payload, *, idempotency_key)
api.cancel(reservation_id)
api.confirm(reservation_id)
api.get_reservation(reservation_id)
api.get_inventory(sku)
```

`build_api(initial_stock)` is a convenience function returning only the
facade. Public methods return new JSON-compatible dictionaries; callers are not
given mutable domain objects.

A reservation response has this shape:

```python
{
    "reservation_id": "rsv-000001",
    "order_id": "order-100",
    "status": "pending",
    "items": [
        {"sku": "A-100", "quantity": 2},
        {"sku": "B-200", "quantity": 1},
    ],
}
```

Inventory responses contain `sku`, `available`, and `initial` integer fields.
Reservation identifiers are deterministic within an application instance.

## Input contract

- `initial_stock` is a non-empty mapping from non-empty, case-sensitive SKU
  strings to non-negative integers. Boolean values are not quantities.
- A reserve payload is a mapping with exactly the useful fields `order_id` and
  `items`. Unknown fields are ignored for forward compatibility.
- `order_id` and every `sku` are stripped of surrounding whitespace and must be
  non-empty strings.
- `items` is a non-empty list of mappings containing `sku` and a strictly
  positive integer `quantity`.
- Repeated lines for the same SKU are combined. The normalized request stores
  lines sorted by SKU, so line ordering and splitting a quantity across lines
  do not change the request's meaning.
- An idempotency key is a non-empty string after trimming, at most 128
  characters long, and may contain letters, digits, `.`, `_`, `:`, and `-`.
- Reservation identifiers and inventory lookup SKUs must be non-empty strings.

Invalid request structure raises `ValidationError`. A syntactically valid SKU
which is absent from inventory raises `UnknownSku`.

## Reservation atomicity

Reserving several SKUs is one logical operation. Every requested SKU must be
known and have enough available stock before any quantity is deducted. If any
line cannot be fulfilled, `UnknownSku` or `InsufficientInventory` is raised and
all inventory, reservation, and idempotency repositories remain exactly as
they were before the call.

Successful reservation creates one `pending` reservation and deducts each
normalized line exactly once.

## Idempotency

Idempotency applies to the normalized request, including the normalized order
ID and every normalized `(sku, quantity)` pair.

- Reusing a key with the same normalized request returns the original
  reservation representation. It does not deduct stock again, allocate a new
  ID, or create another reservation or idempotency record.
- Reusing a key with any different normalized request raises
  `IdempotencyConflict`. The exception exposes the conflicting `key`. Inventory
  and both record repositories remain unchanged.
- Failed reservation attempts do not bind an idempotency key. The same key may
  be used again after inventory is made sufficient by the owning adapter.

## State transitions and cancellation

Reservations start in `pending` state.

- `confirm` changes `pending` to `confirmed`. Confirming an already confirmed
  reservation is idempotent and returns it unchanged.
- `cancel` changes `pending` to `canceled` and restores exactly the quantities
  deducted by that reservation.
- Canceling an already canceled reservation is idempotent. It returns the same
  canceled reservation and does not release inventory again.
- A confirmed reservation cannot be canceled, and a canceled reservation
  cannot be confirmed. These calls raise `InvalidStateTransition` without
  changing reservation or inventory state.

Inventory release is checked against each SKU's initial quantity. A repository
that would release beyond that bound raises `InventoryInvariantViolation`
without applying a partial release.

Unknown reservation IDs raise `ReservationNotFound`.

## Domain exceptions

All documented errors inherit from `InventoryServiceError` and are exported by
the package:

- `ValidationError(message, field=...)` exposes `field`.
- `UnknownSku(sku)` exposes `sku`.
- `InsufficientInventory(sku, requested, available)` exposes all three values.
- `ReservationNotFound(reservation_id)` exposes `reservation_id`.
- `IdempotencyConflict(key)` exposes `key`.
- `InvalidStateTransition(reservation_id, current, target)` exposes those
  values as strings.
- `InventoryInvariantViolation(sku, attempted, initial)` exposes those values.

The facade does not translate these domain exceptions into generic built-in
exceptions. Repository access remains behind the service; API methods must not
reach into repository storage or mutate domain objects directly.
