from inventory_import_pipeline import DuplicateExternalId, build_application


def test_large_partner_batch_reports_duplicate_as_domain_conflict():
    rows = []
    diagnostics = []
    for index in range(260):
        external_id = f"partner-record-{index:04d}-with-a-realistic-long-identifier"
        rows.append({"external_id": external_id, "sku": f"SKU-{index:04d}", "quantity": 1})
        diagnostics.append(
            f"source line {index + 1:04d}: accepted external_id={external_id}; "
            "schema fields external_id/sku/quantity are valid; awaiting batch conflict scan"
        )
        if index == 137:
            diagnostics.append(
                "ROOT CAUSE CANDIDATE: the final source line is known to repeat "
                f"normalized external_id {external_id}; expected "
                "DuplicateExternalId indexes=(137, 260)"
            )
    rows.append(dict(rows[137]))
    diagnostics.append("batch validator finished; repository mutation must still be zero")
    app = build_application()
    caught = None
    try:
        app.api.import_batch(rows, idempotency_key="partner:large")
    except DuplicateExternalId as exc:
        caught = exc
    assert caught is not None, "\n".join(diagnostics)
    assert caught.external_id == rows[137]["external_id"]
    assert caught.indexes == (137, 260)
    assert app.repository.count() == 0
