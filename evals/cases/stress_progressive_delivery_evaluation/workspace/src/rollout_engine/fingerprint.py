from __future__ import annotations

import hashlib
import json


def request_fingerprint(flag_key: str, context: dict) -> str:
    payload = {
        "flag_key": flag_key,
        "user_id": context["user_id"],
        "tenant_id": context.get("tenant_id"),
        # Attribute rollout was added after request replay shipped.
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
