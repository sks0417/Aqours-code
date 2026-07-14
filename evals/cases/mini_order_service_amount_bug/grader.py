from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import emit_result, run_pytest, trace_contains_test_run, trace_tool_count


EXPECTED_TESTS = '''import sys
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
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--final", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    args = parser.parse_args()

    workspace = Path(args.workspace)
    tests_unchanged = (workspace / "tests" / "test_order_service.py").read_text(encoding="utf-8") == EXPECTED_TESTS
    source = (workspace / "src" / "order_service.py").read_text(encoding="utf-8", errors="replace")
    hardcoded = any(token in source for token in ("62.50", "125.00", "Decimal('62.50')", 'Decimal("62.50")'))
    grader_tests = Path(__file__).parent / "grader_tests" / "test_order_service_grader.py"
    result = run_pytest(workspace, ["tests/test_order_service.py", grader_tests])
    saw_test_run = trace_contains_test_run(args.trace)
    passed = (tests_unchanged and not hardcoded
              and result["returncode"] == 0 and saw_test_run)
    if passed:
        reason = ""
        category = None
    elif not tests_unchanged:
        reason = "tests/test_order_service.py was modified"
        category = "constraint_violation"
    elif hardcoded:
        reason = "src/order_service.py appears to hardcode expected test totals"
        category = "constraint_violation"
    elif not saw_test_run:
        reason = "trace did not show a test run"
        category = "test_failure"
    else:
        reason = (result["stdout"] + result["stderr"]).strip()
        category = result["failure_category"] or "test_failure"
    return emit_result(
        passed=passed,
        reason=reason,
        failure_category=category,
        metrics={"tool_calls": trace_tool_count(args.trace),
                 "saw_test_run": saw_test_run, "pytest": result},
    )


if __name__ == "__main__":
    raise SystemExit(main())
