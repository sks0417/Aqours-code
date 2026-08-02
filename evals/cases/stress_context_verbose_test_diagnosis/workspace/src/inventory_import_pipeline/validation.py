from collections.abc import Mapping

from .errors import ImportValidationError
from .models import InventoryRow


def validate_rows(payload):
    if not isinstance(payload, list) or not payload:
        raise ImportValidationError(("payload: must be a non-empty list",))
    rows, errors = [], []
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            errors.append(f"row {index}: must be a mapping")
            continue
        external_id = raw.get("external_id")
        sku = raw.get("sku")
        quantity = raw.get("quantity")
        row_errors = []
        if not isinstance(external_id, str) or not external_id.strip():
            row_errors.append("external_id must be a non-empty string")
        if not isinstance(sku, str) or not sku.strip():
            row_errors.append("sku must be a non-empty string")
        if (isinstance(quantity, bool) or not isinstance(quantity, int)
                or quantity <= 0):
            row_errors.append("quantity must be a positive integer")
        if row_errors:
            errors.extend(f"row {index}: {error}" for error in row_errors)
        else:
            rows.append(InventoryRow(external_id.strip(), sku.strip(), quantity))
    if errors:
        raise ImportValidationError(errors)
    return tuple(rows)
