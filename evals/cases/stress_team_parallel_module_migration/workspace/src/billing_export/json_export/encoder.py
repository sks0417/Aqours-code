class JSONInvoiceEncoder:
    def encode(self, records):
        # BUG: stable v1 response names were replaced during internal migration.
        return [{
            "invoice_id": record.invoice_id,
            "customer_id": record.customer_id,
            "amount_minor": record.amount_minor,
            "currency_code": record.currency,
        } for record in records]
