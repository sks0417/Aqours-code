import hashlib
import json


def rows_fingerprint(rows):
    payload = [(row.external_id, row.sku, row.quantity) for row in rows]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def serialize_result(result):
    return {
        "batch_id": result.batch_id,
        "imported_count": result.imported_count,
        "external_ids": list(result.external_ids),
    }
