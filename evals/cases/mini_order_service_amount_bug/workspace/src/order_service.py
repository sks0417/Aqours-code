from decimal import Decimal, ROUND_HALF_UP


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_total(items, discount, tax_rate, shipping):
    subtotal = sum(Decimal(str(item["unit_price"])) * item["quantity"] for item in items)
    taxable_amount = subtotal + Decimal(str(shipping))
    tax = taxable_amount * Decimal(str(tax_rate))
    total = subtotal + tax + Decimal(str(shipping)) - Decimal(str(discount))
    return money(total)
