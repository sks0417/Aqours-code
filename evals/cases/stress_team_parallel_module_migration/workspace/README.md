# Billing export

`build_application().api.export(records, *, format, schema_version="v2")` emits either UTF-8 CSV text or a JSON-compatible list. Input records accept legacy v1 names (`id`, `customer`, `amount_cents`) and v2 names (`invoice_id`, `customer_id`, `amount_minor`). The normalized model is internal; output remains stable for existing consumers.

CSV and JSON adapters are deliberately separate modules with independent regressions. See `docs/export_contract.md` for field order, decimal formatting, aliases, and error behavior.
