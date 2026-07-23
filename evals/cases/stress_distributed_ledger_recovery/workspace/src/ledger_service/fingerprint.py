from __future__ import annotations

import hashlib
import json


def request_fingerprint(events) -> str:
    payload = [{
        "event_id": event.event_id,
        "transaction_id": event.transaction_id,
        "account_id": event.account_id,
        "partition": event.partition,
        "sequence": event.sequence,
        # The legacy producer predates signed deltas.
        "currency": event.currency,
    } for event in events]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
