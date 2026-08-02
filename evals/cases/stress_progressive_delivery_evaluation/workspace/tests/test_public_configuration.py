from __future__ import annotations

import pytest

from conftest import base_flags
from rollout_engine import ConfigurationError, ContextError, build_application


def test_invalid_replacement_keeps_previous_configuration():
    app = build_application(base_flags())
    invalid = base_flags()
    invalid["new-checkout"]["default_variation"] = "missing"
    with pytest.raises(ConfigurationError):
        app.api.replace_configuration(invalid)
    result = app.api.evaluate(
        "new-checkout", {"user_id": "alice"}, request_id="after:failure")
    assert result["variation"] == "on"


@pytest.mark.parametrize("request_id", ["", "spaces are bad", "x" * 129])
def test_request_id_validation(request_id):
    app = build_application(base_flags())
    with pytest.raises(ContextError) as caught:
        app.api.evaluate(
            "new-checkout", {"user_id": "alice"}, request_id=request_id)
    assert caught.value.field == "request_id"
