from __future__ import annotations

import pytest

from conftest import flag, state
from rollout_engine import ConfigurationError, build_application


def test_failed_replacement_is_fully_atomic():
    app = build_application({"checkout": flag(default="on")})
    app.api.evaluate("checkout", {"user_id": "before"}, request_id="before")
    before = state(app)
    invalid = {"checkout": flag(default="missing")}
    with pytest.raises(ConfigurationError):
        app.api.replace_configuration(invalid)
    assert state(app) == before
    assert app.api.evaluate(
        "checkout", {"user_id": "after"}, request_id="after")["variation"] == "on"


def test_successful_replacement_preserves_history_and_affects_new_requests():
    app = build_application({"checkout": flag(default="off")})
    original = app.api.evaluate(
        "checkout", {"user_id": "same"}, request_id="old")
    app.api.replace_configuration({"checkout": flag(default="on")})
    replay = app.api.evaluate(
        "checkout", {"user_id": "same"}, request_id="old")
    current = app.api.evaluate(
        "checkout", {"user_id": "new"}, request_id="new")
    assert original == replay
    assert current["variation"] == "on"
    assert len(app.api.exposures()) == 2


def test_nested_segment_cycle_is_rejected_without_replacement():
    app = build_application({"checkout": flag(default="on")})
    before = state(app)
    segments = {
        "a": {"conditions": [
            {"attribute": "x", "operator": "segment", "value": "b"}]},
        "b": {"conditions": [
            {"attribute": "x", "operator": "segment", "value": "a"}]},
    }
    with pytest.raises(ConfigurationError) as caught:
        app.api.replace_configuration({"checkout": flag()}, segments)
    assert caught.value.field == "segments"
    assert state(app) == before


def test_invalid_rollout_and_bool_priority_are_rejected():
    bad_weight = {"checkout": flag(rollout=[
        {"variation": "off", "weight": 5000},
        {"variation": "on", "weight": 4999},
    ])}
    with pytest.raises(ConfigurationError):
        build_application(bad_weight)
    bad_priority = {"checkout": flag(rules=[{
        "id": "bad", "priority": True,
        "conditions": [{"attribute": "x", "operator": "eq", "value": 1}],
        "variation": "on",
    }])}
    with pytest.raises(ConfigurationError):
        build_application(bad_priority)
