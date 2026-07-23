from __future__ import annotations

import hashlib
import json


def checkpoint_digest(document: dict) -> str:
    canonical = {
        "balances": document["balances"],
        "event_ids": document["event_ids"],
        "event_count": document["event_count"],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
