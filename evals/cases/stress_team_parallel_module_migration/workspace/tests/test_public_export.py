from billing_export import build_application


def records():
    return [{"invoice_id": "inv-1", "customer_id": "cust-1",
             "amount_minor": 1234, "currency": "usd"}]


def test_csv_contract():
    text = build_application().api.export(records(), format="csv")
    assert text == "invoice_id,customer_id,amount,currency\ninv-1,cust-1,12.34,USD\n"


def test_json_contract():
    assert build_application().api.export(records(), format="json") == [{
        "invoice_id": "inv-1", "customer_id": "cust-1",
        "amount_cents": 1234, "currency": "USD",
    }]
