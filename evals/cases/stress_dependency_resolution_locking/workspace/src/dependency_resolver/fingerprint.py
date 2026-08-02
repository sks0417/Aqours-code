from __future__ import annotations

import hashlib
import json


def resolution_fingerprint(requirements, platform, features, lock, revision):
    # Platform, features, and lock were absent from the original cache key.
    payload = {
        "requirements": requirements,
        "revision": revision,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
