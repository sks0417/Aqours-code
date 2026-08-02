# Delivery contract

Channel order is stable: try the normalized primary channel first, followed by normalized fallback channels in request order. Remove duplicates while keeping their first position. A channel disabled by application configuration is never eligible, including when explicitly requested as the primary. Missing providers are treated like temporarily unavailable providers.

Only `ProviderUnavailable` permits trying the next eligible channel. `PermanentProviderError` represents rejected content, invalid provider credentials, or another definitive response; it must escape immediately and must never cause fallback delivery. If every eligible attempt is unavailable, raise `DeliveryFailed` and include the attempted channel tuple.

Idempotency is keyed by the caller's `idempotency_key`. A repeat with the same normalized request returns the original receipt and performs no rate-limit consumption or provider call. Reusing a key for a different normalized request raises `IdempotencyConflict`. Failed attempts are not remembered; a later retry may succeed.

Normalization trims surrounding whitespace from textual fields and lowercases channel names. The fingerprint includes notification id, recipient, message, primary channel, and the ordered de-duplicated fallback list. Unknown input fields do not affect it.

Successful receipts are immutable snapshots. The caller may mutate a returned dictionary without changing the value returned by a later idempotent retry.
