from __future__ import annotations

from copy import deepcopy

import pytest

from conftest import flag, state
from rollout_engine import RequestConflict, UnknownFlag, build_application


def test_attributes_are_part_of_request_fingerprint(make_application):
    app = make_application()
    app.api.evaluate(
        "checkout", {"user_id": "u", "attributes": {"region": "us"}},
        request_id="same")
    before = state(app)
    with pytest.raises(RequestConflict) as caught:
        app.api.evaluate(
            "checkout", {"user_id": "u", "attributes": {"region": "eu"}},
            request_id="same")
    assert caught.value.request_id == "same"
    assert state(app) == before


def test_failed_evaluation_binds_and_exposes_nothing(make_application):
    app = make_application()
    before = state(app)
    with pytest.raises(UnknownFlag):
        app.api.evaluate("missing", {"user_id": "u"}, request_id="failure")
    assert state(app) == before


def test_returned_results_and_exposures_are_fresh_copies():
    flags = {"checkout": flag(
        default="on", variations={"off": False, "on": {"color": "blue"}})}
    app = build_application(flags)
    first = app.api.evaluate("checkout", {"user_id": "u"}, request_id="copy")
    first["value"]["color"] = "forged"
    first["variation"] = "forged"
    exposed = app.api.exposures()
    exposed[0]["variation"] = "forged"
    replay = app.api.evaluate("checkout", {"user_id": "u"}, request_id="copy")
    assert replay["variation"] == "on"
    assert replay["value"] == {"color": "blue"}
    assert app.api.exposures()[0]["variation"] == "on"


def test_request_reuse_for_other_flag_is_side_effect_free():
    app = build_application({"a": flag(), "b": flag(default="on")})
    app.api.evaluate("a", {"user_id": "u"}, request_id="one")
    before = deepcopy(state(app))
    with pytest.raises(RequestConflict):
        app.api.evaluate("b", {"user_id": "u"}, request_id="one")
    assert state(app) == before
