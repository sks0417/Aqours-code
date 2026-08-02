from dataclasses import dataclass


@dataclass(frozen=True)
class InvoiceRecord:
    invoice_id: str
    customer_id: str
    amount_minor: int
    currency: str
