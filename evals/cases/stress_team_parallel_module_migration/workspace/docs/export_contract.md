# Export contract

Every record normalizes to invoice id, customer id, integer minor amount, and uppercase three-letter currency. A record may use all v1 aliases or all v2 names. If both aliases for one logical field are supplied with different values, raise `ExportValidationError`. Unknown fields are ignored.

CSV output has the exact header `invoice_id,customer_id,amount,currency` and one row per input record. `amount` is a two-decimal major-unit string: 5 becomes `0.05`, 1234 becomes `12.34`, and negative values are allowed. Use standard CSV quoting. Output ends with a newline.

JSON output is a list of dictionaries in input order. Each dictionary has exactly `invoice_id`, `customer_id`, `amount_cents`, and `currency`; `amount_cents` is the normalized integer. The v2 schema version changes accepted input aliases, not this long-standing response shape.

`format` accepts only lowercase `csv` and `json`; `schema_version` accepts `v1` or `v2`. Validation occurs before adapter encoding. Empty lists are valid and produce the CSV header or an empty JSON list.
