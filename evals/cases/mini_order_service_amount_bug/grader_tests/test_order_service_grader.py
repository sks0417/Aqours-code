import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

workspace = Path(os.environ["EVAL_GRADING_WORKSPACE"])
sys.path.insert(0, str(workspace / "src"))

from order_service import calculate_total


def expected_total(items, discount, tax_rate, shipping):
    subtotal = sum(Decimal(str(item["unit_price"])) * item["quantity"] for item in items)
    taxable = max(Decimal("0"), subtotal - Decimal(str(discount)))
    tax = taxable * Decimal(str(tax_rate))
    total = taxable + tax + Decimal(str(shipping))
    return Decimal(total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def test_discount_tax_shipping_order_with_different_numbers():
    items = [
        {"sku": "X", "quantity": 3, "unit_price": "12.49"},
        {"sku": "Y", "quantity": 2, "unit_price": "4.10"},
    ]
    assert calculate_total(items, "6.25", "0.0825", "9.99") == expected_total(
        items, "6.25", "0.0825", "9.99")


def test_empty_items_and_zero_discount_do_not_tax_shipping():
    assert calculate_total([], "0.00", "0.25", "4.99") == Decimal("4.99")


def test_fractional_prices_round_half_up():
    items = [{"sku": "P", "quantity": 1, "unit_price": "1.005"}]
    assert calculate_total(items, "0.00", "0.00", "0.00") == Decimal("1.01")
