# Import contract

A batch may not repeat an `external_id`, even when the repeated rows otherwise match. Such a batch raises `DuplicateExternalId`; its `external_id` attribute is the normalized duplicated identifier and its `indexes` attribute is the tuple of every zero-based occurrence. Nothing is inserted and no idempotency record is created.

An identifier already present from an earlier successful batch also raises `DuplicateExternalId`. In that case `indexes` contains only the index from the new request.

Schema failures are aggregated into `ImportValidationError.errors`, a tuple of strings in row order. Duplicate identifiers are domain conflicts, not schema errors. Quantity rejects booleans, zero, negatives, strings, and floats.

An idempotency key identifies the normalized ordered batch. Repeating the same key and same batch returns a detached copy of the original result without repository writes. Reusing the key with different content raises `IdempotencyConflict`. Failed imports do not consume keys.

Batch ids are deterministic within an application: `batch-0001`, `batch-0002`, and so on for successful new imports only. Unknown fields do not affect normalized content or its fingerprint.
