# Inventory import pipeline

`build_application()` returns an in-memory application with `api`, `repository`, and `dedupe` attributes. The stable entry point is `app.api.import_batch(payload, *, idempotency_key)` where payload is a non-empty list of row mappings.

Each row requires a trimmed non-empty `external_id` and `sku`, plus a positive integer `quantity`. Unknown row fields are ignored. A successful result contains exactly `batch_id`, `imported_count`, and `external_ids` in input order.

The operation is atomic: validation, duplicate checks, and idempotency conflict checks happen before repository mutation. See `docs/import_contract.md` for exact failure boundaries.

The intentionally verbose public integration test mirrors a real CI job that emits one diagnostic entry per source row. Use a focused selector once the failure family is known.
