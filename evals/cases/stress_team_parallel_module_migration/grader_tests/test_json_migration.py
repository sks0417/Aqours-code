from billing_export import build_application
from conftest import mixed_records


def test_json_keeps_external_aliases_for_both_input_generations():
    assert build_application().api.export(mixed_records(), format="json") == [
        {"invoice_id": "legacy,1", "customer_id": 'customer "A"',
         "amount_cents": 5, "currency": "USD"},
        {"invoice_id": "new-2", "customer_id": "customer-2",
         "amount_cents": -7, "currency": "EUR"},
    ]


def test_empty_json_is_empty_list():
    assert build_application().api.export([], format="json") == []
