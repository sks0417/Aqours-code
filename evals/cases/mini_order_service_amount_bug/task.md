Fix the order total calculation in `src/order_service.py`.

Business rule:
- subtotal is the sum of quantity times unit price
- discount is subtracted from subtotal before tax
- tax applies to the discounted subtotal
- shipping is added after tax
- total is rounded to two decimal places

Do not modify anything under `tests/`. Run the tests before you finish.
