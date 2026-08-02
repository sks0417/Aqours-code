class JSONInvoiceEncoder:
    def encode(self, records):
        return [{
            "invoice_id": record.invoice_id,
            "customer_id": record.customer_id,
            "amount_cents": record.amount_minor,
            "currency": record.currency,
        } for record in records]
