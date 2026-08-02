from __future__ import annotations

from conftest import base_flags
from rollout_engine import build_application


def test_user_target_and_default_shapes():
    app = build_application(base_flags())
    targeted = app.api.evaluate(
        "new-checkout", {"user_id": " alice "}, request_id="req:target")
    default = app.api.evaluate(
        "new-checkout", {"user_id": "bob"}, request_id="req:default")

    assert targeted == {
        "flag_key": "new-checkout", "variation": "on",
        "value": True, "reason": "target:user",
    }
    assert default == {
        "flag_key": "new-checkout", "variation": "off",
        "value": False, "reason": "default",
    }


def test_rule_requires_every_condition():
    app = build_application(base_flags())
    partial = app.api.evaluate(
        "new-checkout",
        {"user_id": "bob", "attributes": {"plan": "pro", "app_version": "1.0.0"}},
        request_id="req:partial")
    complete = app.api.evaluate(
        "new-checkout",
        {"user_id": "carol", "attributes": {"plan": "pro", "app_version": "2.1.0"}},
        request_id="req:complete")
    assert partial["variation"] == "off"
    assert complete["reason"] == "rule:modern-pro"


def test_same_request_is_exactly_once():
    app = build_application(base_flags())
    context = {"user_id": "alice", "attributes": {"region": "us"}}
    first = app.api.evaluate("new-checkout", context, request_id="req:once")
    second = app.api.evaluate(
        "new-checkout",
        {"attributes": {"region": "us"}, "user_id": " alice "},
        request_id=" req:once ")
    assert first == second
    assert len(app.api.exposures()) == 1
