import csv
import io


class CSVInvoiceEncoder:
    def encode(self, records):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        # BUG: v2 internal names leaked into the stable CSV contract.
        writer.writerow(["invoice_id", "customer_id", "amount_minor", "currency"])
        for record in records:
            writer.writerow([record.invoice_id, record.customer_id,
                             record.amount_minor, record.currency])
        return stream.getvalue()
