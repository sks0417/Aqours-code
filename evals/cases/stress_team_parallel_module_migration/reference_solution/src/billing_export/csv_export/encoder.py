import csv
import io


class CSVInvoiceEncoder:
    def encode(self, records):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["invoice_id", "customer_id", "amount", "currency"])
        for record in records:
            sign = "-" if record.amount_minor < 0 else ""
            absolute = abs(record.amount_minor)
            amount = f"{sign}{absolute // 100}.{absolute % 100:02d}"
            writer.writerow([record.invoice_id, record.customer_id, amount, record.currency])
        return stream.getvalue()
