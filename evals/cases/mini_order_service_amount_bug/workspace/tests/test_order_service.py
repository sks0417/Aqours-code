import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from order_service import calculate_total


def test_discount_applies_before_tax_and_shipping_after_tax():
    items = [
        {"sku": "A", "quantity": 2, "unit_price": "20.00"},
        {"sku": "B", "quantity": 1, "unit_price": "15.00"},
    ]
    assert calculate_total(items, discount="5.00", tax_rate="0.10", shipping="7.50") == Decimal("62.50")


def test_shipping_is_not_taxed():
    items = [{"sku": "A", "quantity": 1, "unit_price": "100.00"}]
    assert calculate_total(items, discount="0.00", tax_rate="0.05", shipping="20.00") == Decimal("125.00")


def test_rounds_half_up_to_two_decimals():
    items = [{"sku": "A", "quantity": 3, "unit_price": "0.333"}]
    assert calculate_total(items, discount="0.00", tax_rate="0.00", shipping="0.00") == Decimal("1.00")


def test_discount_can_reduce_taxable_amount_to_zero():
    items = [{"sku": "A", "quantity": 1, "unit_price": "10.00"}]
    assert calculate_total(items, discount="10.00", tax_rate="0.25", shipping="3.00") == Decimal("3.00")
