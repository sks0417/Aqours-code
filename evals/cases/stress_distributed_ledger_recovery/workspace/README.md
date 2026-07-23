# Distributed Ledger Projection Service

This project is an in-memory boundary for ingesting ordered ledger events from
several partitions, maintaining account projections, and recovering them from a
verified checkpoint plus the durable event tail. Persistence adapters are kept
separate from orchestration so the contracts can later be used with durable
stores.

Run the public suite from the workspace root with `python -m pytest -q`.

## Public API

`ledger_service.bootstrap.build_application(initial_accounts)` returns a
`LedgerApplication`. Its `api` facade is the supported interface; repositories
are exposed on the application only for diagnostics and adapter/recovery tests.

```python
api.ingest(payload, *, idempotency_key)
api.get_balance(account_id)
api.get_partition_sequence(partition)
api.create_checkpoint()
api.recover()
```

Returned values are fresh JSON-compatible dictionaries. The API never exposes
mutable domain objects. Domain exceptions are not translated to built-ins.

`initial_accounts` is a non-empty mapping. Each key is a non-empty,
case-sensitive account ID and each value is a mapping containing exactly useful
fields `currency` and `balance`; unknown fields are ignored. Currency is a
three-letter ASCII code normalized to uppercase. Balance is a non-negative
integer; booleans are invalid.

## Event normalization

An ingestion payload is a non-empty list of mappings. Unknown fields are
ignored. Every event has:

- non-empty trimmed strings `event_id`, `transaction_id`, `account_id`, and
  `partition`;
- a positive integer `sequence` (booleans are invalid);
- a non-zero integer `delta` (booleans are invalid); and
- a three-letter currency normalized to uppercase.

The normalized request is sorted by `(partition, sequence, event_id)`. Event IDs
must be unique within a request. An idempotency key is a trimmed non-empty
string, at most 128 characters, containing only letters, digits, `.`, `_`, `:`,
and `-`.

Validation failures raise `ValidationError(field=...)`. A syntactically valid
unknown account raises `UnknownAccount(account_id)`. A currency different from
the account currency raises `CurrencyMismatch(account_id, expected, actual)`.

## Atomic ingestion and partition ordering

One payload is one transaction across all partitions and accounts. For each
partition, submitted sequences must be exactly contiguous from the repository's
current sequence plus one. Multiple events for a partition in one batch must
remain contiguous after normalization. Any gap or stale sequence raises
`SequenceConflict(partition, expected, actual)`.

Every account must exist, currencies must match, and every projected balance
must remain non-negative after the complete batch. `InsufficientFunds` exposes
`account_id`, `attempted`, and `available`.

If validation, sequence checking, event insertion, or projection fails, event
store, balances, partition sequences, receipts, idempotency bindings, and the
next batch identifier all remain exactly unchanged. Successful ingestion appends
each event once, updates all balances and sequences once, and creates one
receipt with deterministic ID `batch-000001`, `batch-000002`, and so on.

The receipt shape is:

```python
{
    "batch_id": "batch-000001",
    "event_ids": ["evt-1", "evt-2"],
    "balances": {"acct-a": 7, "acct-b": 13},
    "sequences": {"east": 2, "west": 4},
}
```

Keys in all returned mappings are sorted. `event_ids` follow normalized request
order.

## Exactly-once and idempotency

Idempotency fingerprints include every normalized field: event ID, transaction
ID, account ID, partition, sequence, delta, and normalized currency.

- Reusing a key with the same normalized request returns the original receipt
  without allocating a batch ID or changing any repository.
- Reusing a key with any different normalized request raises
  `IdempotencyConflict(key)` and changes nothing.
- Reusing an event ID under a different key raises `DuplicateEvent(event_id)`
  and changes nothing.
- A failed attempt never binds its key and never consumes a batch ID. The same
  key can be retried after the underlying state is corrected.

## Checkpoints and recovery

`create_checkpoint()` captures fresh copies of balances, partition sequences,
the ordered event IDs, and `event_count`. Its SHA-256 digest covers all four
fields using canonical JSON. Creating a checkpoint does not mutate ingestion
state.

`recover()` verifies the latest checkpoint digest before changing live state,
restores that checkpoint, and replays every event after `event_count` in durable
order. If there is no checkpoint it rebuilds from the immutable initial account
state and all events. Replay enforces the same account, currency, funds, event
order, and per-partition sequence invariants as live ingestion.

A malformed checkpoint, digest mismatch, impossible event count, event-ID
prefix mismatch, or invalid replay raises `CheckpointCorrupt` and leaves live
balances and sequences unchanged. A successful recovery returns current
`balances`, `sequences`, and `event_count`. Recovery never appends events,
creates receipts, changes idempotency bindings, or consumes a batch ID.

## Exceptions and architecture

All errors inherit from `LedgerServiceError` and are exported by the package:
`ValidationError`, `UnknownAccount`, `CurrencyMismatch`, `InsufficientFunds`,
`SequenceConflict`, `DuplicateEvent`, `IdempotencyConflict`, and
`CheckpointCorrupt`.

The facade delegates to `LedgerService` and `RecoveryService`. It must not reach
into repository storage. Avoid test-specific branches, dynamic execution, or
coupling production code to grader paths.
