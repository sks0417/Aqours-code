from collections.abc import Mapping

from .errors import ExportValidationError
from .models import InvoiceRecord


def _alias(record, old, new):
    if old in record and new in record and record[old] != record[new]:
        raise ExportValidationError(f"conflicting aliases: {old}, {new}")
    return record[new] if new in record else record.get(old)


def normalize_records(records):
    if not isinstance(records, list):
        raise ExportValidationError("records must be a list")
    normalized = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ExportValidationError("record must be a mapping")
        invoice_id = _alias(record, "id", "invoice_id")
        customer_id = _alias(record, "customer", "customer_id")
        amount = _alias(record, "amount_cents", "amount_minor")
        currency = record.get("currency")
        if not isinstance(invoice_id, str) or not invoice_id.strip():
            raise ExportValidationError("invalid invoice id")
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ExportValidationError("invalid customer id")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ExportValidationError("invalid amount")
        if (not isinstance(currency, str) or len(currency.strip()) != 3
                or not currency.strip().isalpha()):
            raise ExportValidationError("invalid currency")
        normalized.append(InvoiceRecord(invoice_id.strip(), customer_id.strip(),
                                        amount, currency.strip().upper()))
    return tuple(normalized)
