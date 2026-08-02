import csv
import io

from billing_export import build_application
from conftest import mixed_records


def test_csv_exact_header_money_and_quoting():
    text = build_application().api.export(mixed_records(), format="csv")
    assert text.endswith("\n")
    assert list(csv.reader(io.StringIO(text))) == [
        ["invoice_id", "customer_id", "amount", "currency"],
        ["legacy,1", 'customer "A"', "0.05", "USD"],
        ["new-2", "customer-2", "-0.07", "EUR"],
    ]


def test_empty_csv_is_header_only():
    assert build_application().api.export([], format="csv") == (
        "invoice_id,customer_id,amount,currency\n")
